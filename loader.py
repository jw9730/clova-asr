"""
Copyright 2019-present NAVER Corp.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

#-*- coding: utf-8 -*-

import os
import sys
import math
import wavio
import time
import torch
import random
import threading
import logging
from torch.utils.data import Dataset, DataLoader
import numpy
from specaugment import spec_augment_pytorch, melscale_pytorch, trim


logger = logging.getLogger('root')
FORMAT = "[%(asctime)s %(filename)s:%(lineno)s - %(funcName)s()] %(message)s"
logging.basicConfig(stream=sys.stdout, level=logging.DEBUG, format=FORMAT)
logger.setLevel(logging.INFO)

PAD = 0
N_FFT = 512
SAMPLE_RATE = 16000

target_dict = dict()


def load_targets(path):
    with open(path, 'r') as f:
        for no, line in enumerate(f):
            key, target = line.strip().split(',')
            target_dict[key] = target


def get_spectrogram_feature(cfg_data, filepath, train_mode=False):

    use_mel_scale = cfg_data["use_mel_scale"]
    cfg_spec_augment = cfg_data["spec_augment"]
    use_specaug = cfg_spec_augment["use"]
    
    (rate, width, sig) = wavio.readwav(filepath)
    sig = sig.ravel()
    sig = trim.trim(sig)
    stft = torch.stft(torch.FloatTensor(sig),
                      N_FFT,
                      hop_length=int(0.01*SAMPLE_RATE),
                      win_length=int(0.030*SAMPLE_RATE),
                      window=torch.hamming_window(int(0.030*SAMPLE_RATE)),
                      center=False,
                      normalized=False,
                      onesided=True)
    stft = (stft[:,:,0].pow(2) + stft[:,:,1].pow(2)).pow(0.5)

    if use_mel_scale:
        amag = stft.clone().detach()
        amag = amag.view(-1, amag.shape[0], amag.shape[1])  # reshape spectrogram shape to [batch_size, time, frequency]
        mel = melscale_pytorch.mel_scale(amag, sample_rate=SAMPLE_RATE, n_mels=N_FFT//2+1)  # melspec with same shape
        if use_specaug and train_mode:
            specaug_prob = 1  # augment probability
            if numpy.random.uniform(0, 1) < specaug_prob:
                # apply augment
                mel = spec_augment_pytorch.spec_augment(mel, time_warping_para=80, frequency_masking_para=54,
                                                time_masking_para=100, frequency_mask_num=1, time_mask_num=1)
        feat = mel.view(mel.shape[1], mel.shape[2])  # squeeze back to [frequency, time]
        feat = feat.transpose(0, 1).clone().detach()
        del stft, amag, mel
    else:
        # use baseline feature
        amag = stft.numpy()
        feat = torch.FloatTensor(amag)
        feat = torch.FloatTensor(feat).transpose(0, 1)
        del stft, amag

    return feat

def spec_augment_wrapper(mel, cfg_spec_augment):
    aug_mel = spec_augment_pytorch.spec_augment(
        mel, 
        time_warping_para=cfg_spec_augment["time_warping_para"],
        frequency_masking_para=cfg_spec_augment["frequency_masking_para"],
        time_masking_para=cfg_spec_augment["time_masking_para"],
        frequency_mask_num=cfg_spec_augment["frequency_mask_num"],
        time_mask_num=cfg_spec_augment["time_mask_num"])
    return aug_mel

def get_script(filepath, bos_id, eos_id):
    key = filepath.split('/')[-1].split('.')[0]
    script = target_dict[key]
    tokens = script.split(' ')
    result = list()
    result.append(bos_id)
    for i in range(len(tokens)):
        if len(tokens[i]) > 0:
            result.append(int(tokens[i]))
    result.append(eos_id)
    return result


class BaseDataset(Dataset):
    def __init__(self, cfg_data, wav_paths, script_paths, bos_id=1307, eos_id=1308, train_mode=False):
        self.cfg_data = cfg_data
        self.wav_paths = wav_paths
        self.script_paths = script_paths
        self.bos_id, self.eos_id = bos_id, eos_id
        self.train_mode = train_mode

    def __len__(self):
        return len(self.wav_paths)

    def count(self):
        return len(self.wav_paths)

    def getitem(self, idx):
        feat = get_spectrogram_feature(self.cfg_data, self.wav_paths[idx], train_mode=self.train_mode)
        script = get_script(self.script_paths[idx], self.bos_id, self.eos_id)
        return feat, script


def _collate_fn(batch):
    def seq_length_(p):
        return len(p[0])

    def target_length_(p):
        return len(p[1])

    seq_lengths = [len(s[0]) for s in batch]
    target_lengths = [len(s[1]) for s in batch]

    max_seq_sample = max(batch, key=seq_length_)[0]
    max_target_sample = max(batch, key=target_length_)[1]

    max_seq_size = max_seq_sample.size(0)
    max_target_size = len(max_target_sample)

    feat_size = max_seq_sample.size(1)
    batch_size = len(batch)

    seqs = torch.zeros(batch_size, max_seq_size, feat_size)

    targets = torch.zeros(batch_size, max_target_size).to(torch.long)
    targets.fill_(PAD)

    for x in range(batch_size):
        sample = batch[x]
        tensor = sample[0]
        target = sample[1]
        seq_length = tensor.size(0)
        seqs[x].narrow(0, 0, seq_length).copy_(tensor)
        targets[x].narrow(0, 0, len(target)).copy_(torch.LongTensor(target))

    return seqs, targets, seq_lengths, target_lengths


class BaseDataLoader(threading.Thread):
    def __init__(self, dataset, queue, batch_size, thread_id):
        threading.Thread.__init__(self)
        self.collate_fn = _collate_fn
        self.dataset = dataset
        self.queue = queue
        self.index = 0
        self.batch_size = batch_size
        self.dataset_count = dataset.count()
        self.thread_id = thread_id

    def count(self):
        return math.ceil(self.dataset_count / self.batch_size)

    def create_empty_batch(self):
        seqs = torch.zeros(0, 0, 0)
        targets = torch.zeros(0, 0).to(torch.long)
        seq_lengths = list()
        target_lengths = list()
        return seqs, targets, seq_lengths, target_lengths

    def run(self):
        logger.debug('loader %d start' % (self.thread_id))
        while True:
            items = list()

            for i in range(self.batch_size): 
                if self.index >= self.dataset_count:
                    break

                items.append(self.dataset.getitem(self.index))
                self.index += 1

            if len(items) == 0:
                batch = self.create_empty_batch()
                self.queue.put(batch)
                break

            random.shuffle(items)

            batch = self.collate_fn(items)
            self.queue.put(batch)
        logger.debug('loader %d stop' % (self.thread_id))


class MultiLoader():
    def __init__(self, dataset_list, queue, batch_size, worker_size):
        self.dataset_list = dataset_list
        self.queue = queue
        self.batch_size = batch_size
        self.worker_size = worker_size
        self.loader = list()

        for i in range(self.worker_size):
            self.loader.append(BaseDataLoader(self.dataset_list[i], self.queue, self.batch_size, i))

    def start(self):
        for i in range(self.worker_size):
            self.loader[i].start()

    def join(self):
        for i in range(self.worker_size):
            self.loader[i].join()

