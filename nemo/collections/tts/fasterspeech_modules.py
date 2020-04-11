# Copyright 2020 NVIDIA. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import math
import time
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.nn import functional as F

import nemo
from nemo.backends.pytorch import nm as nemo_nm
from nemo.backends.pytorch.nm import DataLayerNM, LossNM
from nemo.collections import asr as nemo_asr
from nemo.collections import tts as nemo_tts
from nemo.collections.asr.parts import AudioDataset, WaveformFeaturizer
from nemo.core.neural_types import (
    AudioSignal,
    ChannelType,
    EmbeddedTextType,
    EncodedRepresentation,
    LengthsType,
    MaskType,
    MelSpectrogramType,
    NeuralType,
)
from nemo.utils.decorators import add_port_docs

__all__ = ['FasterSpeechDataLayer', 'FasterSpeech', 'FasterSpeechDurLoss', 'FasterSpeechMelLoss']


class _Ops:
    @staticmethod
    def merge(tensors, value=0.0, dtype=torch.float):
        max_len = max(tensor.shape[0] for tensor in tensors)
        new_tensors = []
        for tensor in tensors:
            pad = (2 * len(tensor.shape)) * [0]
            pad[-1] = max_len - tensor.shape[0]
            new_tensors.append(F.pad(tensor, pad=pad, value=value))
        return torch.stack(new_tensors).to(dtype=dtype)

    @staticmethod
    def make_mask(lengths):
        device = lengths.device if torch.is_tensor(lengths) else 'cpu'
        return _Ops.merge([torch.ones(length, device=device) for length in lengths], value=0, dtype=torch.bool)

    @staticmethod
    def interleave(x, y):
        xy = torch.stack([x[:-1], y], dim=1).view(-1)
        xy = F.pad(xy, pad=[0, 1], value=x[-1])
        return xy


class FasterSpeechDataset:
    def __init__(self, audio_dataset, durs_file, durs_type='full-pad', speaker_table=None, speaker_embs=None):
        self._audio_dataset = audio_dataset
        self._durs = np.load(durs_file, allow_pickle=True)
        self._durs_type = durs_type
        self._speakers = None
        self._speaker_embs = None

        if speaker_table is not None:
            self._speakers = {sid: i for i, sid in enumerate(pd.read_csv(speaker_table, sep='\t').index)}
            self._speaker_embs = np.load(speaker_embs, allow_pickle=True)

    def __getitem__(self, index):
        id_, audio, audio_len, text, text_len, speaker = self._audio_dataset[index]
        example = dict(audio=audio, audio_len=audio_len, text=text, text_len=text_len)

        if self._durs_type == 'pad':
            dur = self._durs[id_]
            example['dur'] = torch.tensor(dur, dtype=torch.long)
        elif self._durs_type == 'full-pad':
            blank, dur = self._durs[id_]
            example['blank'] = torch.tensor(blank, dtype=torch.long)
            example['dur'] = torch.tensor(dur, dtype=torch.long)
        else:
            raise ValueError("Wrong durations handling type.")

        if self._speakers is not None:
            example['speaker'] = self._speakers[speaker]
            example['speaker_emb'] = torch.tensor(self._speaker_embs[example['speaker']])

        return example

    def __len__(self):
        return len(self._audio_dataset)


class SuperSmartSampler(torch.utils.data.distributed.DistributedSampler):
    def __init__(self, *args, **kwargs):
        self.lengths = kwargs.pop('lengths')
        self.batch_size = kwargs.pop('batch_size')

        super().__init__(*args, **kwargs)

    def __iter__(self):
        indices = list(super().__iter__())

        indices.sort(key=lambda i: self.lengths[i])

        batches = []
        for i in range(0, len(indices), self.batch_size):
            batches.append(indices[i : i + self.batch_size])

        g = torch.Generator()
        g.manual_seed(self.epoch)
        b_indices = torch.randperm(len(batches), generator=g).tolist()

        for b_i in b_indices:
            yield from batches[b_i]


class FasterSpeechDataLayer(DataLayerNM):
    """Data Layer for Faster Speech model.

    Basically, replicated behavior from AudioToText Data Layer, zipped with ground truth durations for additional loss.

    """

    @property
    @add_port_docs
    def output_ports(self):
        """Returns definitions of module output ports."""
        return dict(
            audio=NeuralType(('B', 'T'), AudioSignal(freq=self._sample_rate)),
            audio_len=NeuralType(tuple('B'), LengthsType()),
            text=NeuralType(('B', 'T'), EmbeddedTextType()),
            text_mask=NeuralType(('B', 'T'), MaskType()),
            dur=NeuralType(('B', 'T'), LengthsType()),
            text_rep=NeuralType(('B', 'T'), LengthsType()),
            text_rep_mask=NeuralType(('B', 'T'), MaskType()),
            speaker=NeuralType(('B',), EmbeddedTextType(), optional=True),
            speaker_emb=NeuralType(('B', 'T'), EncodedRepresentation(), optional=True),
        )

    def __init__(
        self,
        manifests,
        durs_file,
        labels,
        durs_type='full-pad',
        speaker_table=None,
        speaker_embs=None,
        batch_size=32,
        sample_rate=16000,
        int_values=False,
        bos_id=None,
        eos_id=None,
        pad_id=None,
        blank_id=None,
        min_duration=0.1,
        max_duration=None,
        normalize_transcripts=True,
        trim_silence=False,
        load_audio=True,
        drop_last=False,
        shuffle=True,
        num_workers=0,
        sampler_type='default',
    ):
        super().__init__()

        # Set up dataset.
        self._featurizer = WaveformFeaturizer(sample_rate=sample_rate, int_values=int_values, augmentor=None)
        dataset_params = {
            'manifest_filepath': manifests,
            'labels': labels,
            'featurizer': self._featurizer,
            'max_duration': max_duration,
            'min_duration': min_duration,
            'normalize': normalize_transcripts,
            'trim': trim_silence,
            'bos_id': bos_id,
            'eos_id': eos_id,
            'load_audio': load_audio,
            'add_id': True,
            'add_speaker': True,
        }
        audio_dataset = AudioDataset(**dataset_params)
        self._dataset = FasterSpeechDataset(audio_dataset, durs_file, durs_type, speaker_table, speaker_embs)
        self._durs_type = durs_type
        self._pad_id = pad_id
        self._blank_id = blank_id
        self._space_id = labels.index(' ')
        self._sample_rate = sample_rate
        self._load_audio = load_audio

        sampler = None
        if self._placement == nemo.core.DeviceType.AllGpu:
            if sampler_type == 'default':
                sampler = torch.utils.data.distributed.DistributedSampler(self._dataset)
            elif sampler_type == 'super-smart':
                sampler = SuperSmartSampler(
                    dataset=self._dataset,
                    lengths=[e.duration for e in audio_dataset.collection],
                    batch_size=batch_size,
                )
            else:
                raise ValueError("Invalid sample type.")

        self._dataloader = torch.utils.data.DataLoader(
            dataset=self._dataset,
            batch_size=batch_size,
            collate_fn=self._collate,
            drop_last=drop_last,
            shuffle=shuffle if sampler is None else False,
            sampler=sampler,
            num_workers=num_workers,
        )

    def _collate(self, batch):
        batch = {key: [example[key] for example in batch] for key in batch[0]}

        if self._load_audio:
            audio = _Ops.merge(batch['audio'])
            audio_len = torch.tensor(batch['audio_len'], dtype=torch.long)
        else:
            audio, audio_len = None, None

        if self._durs_type == 'pad':
            text = [F.pad(text, pad=[1, 1], value=self._space_id) for text in batch['text']]
            text = _Ops.merge(text, value=self._pad_id, dtype=torch.long)
            # noinspection PyTypeChecker
            text_mask = _Ops.make_mask([text_len + 2 for text_len in batch['text_len']])
            dur = _Ops.merge(batch['dur'], dtype=torch.long)
        elif self._durs_type == 'full-pad':
            # `text`
            text = [
                _Ops.interleave(x=torch.empty(len(text) + 1, dtype=torch.long).fill_(self._blank_id), y=text)
                for text in batch['text']
            ]
            text = _Ops.merge(text, value=self._pad_id, dtype=torch.long)

            # `text_mask`
            # noinspection PyTypeChecker
            text_mask = _Ops.make_mask([text_len * 2 + 1 for text_len in batch['text_len']])

            # `dur`
            blank, dur = batch['blank'], batch['dur']
            dur = _Ops.merge([_Ops.interleave(b, d) for b, d in zip(blank, dur)], dtype=torch.long)
        else:
            raise ValueError("Wrong durations handling type.")

        text_rep = _Ops.merge(
            tensors=[torch.repeat_interleave(text1, dur1) for text1, dur1 in zip(text, dur)], dtype=torch.long,
        )
        text_rep_mask = _Ops.make_mask(dur.sum(-1))

        speaker, speaker_emb = None, None
        if 'speaker' in batch:
            speaker = torch.tensor(batch['speaker'], dtype=torch.long)
            speaker_emb = _Ops.merge(batch['speaker_emb'], dtype=torch.float)

        assert audio is None or audio.shape[-1] == audio_len.max()
        assert text.shape == text_mask.shape, f'{text.shape} vs {text_mask.shape}'
        assert text.shape == dur.shape, f'{text.shape} vs {dur.shape}'

        return audio, audio_len, text, text_mask, dur, text_rep, text_rep_mask, speaker, speaker_emb

    def __len__(self) -> int:
        return len(self._dataset)

    @property
    def dataset(self) -> Optional[torch.utils.data.Dataset]:
        return None

    @property
    def data_iterator(self) -> Optional[torch.utils.data.DataLoader]:
        return self._dataloader

    @property
    def n_speakers(self):
        # noinspection PyProtectedMember
        return len(self._dataset._speaker_table)


class FasterSpeech(nemo_nm.TrainableNM):
    """FasterSpeech TTS Model"""

    @property
    @add_port_docs
    def input_ports(self):
        """Returns definitions of module input ports."""
        return dict(
            text=NeuralType(('B', 'T'), EmbeddedTextType()),
            text_mask=NeuralType(('B', 'T'), MaskType()),
            text_rep=NeuralType(('B', 'T'), LengthsType(), optional=True),
            text_rep_mask=NeuralType(('B', 'T'), MaskType(), optional=True),
            speaker_emb=NeuralType(('B', 'T'), EncodedRepresentation(), optional=True),
        )

    @property
    @add_port_docs
    def output_ports(self):
        """Returns definitions of module output ports."""
        return dict(pred=NeuralType(('B', 'T', 'D'), ChannelType()), len=NeuralType(('B',), LengthsType()))

    def __init__(
        self,
        n_vocab: int,
        d_char: int,
        pad_id: int,
        jasper_kwargs: Dict[str, Any],
        d_out: int,
        d_speaker_emb: Optional[int] = None,
        d_speaker: Optional[int] = None,
    ):
        super().__init__()

        # Embedding for input text
        self.text_emb = nn.Embedding(n_vocab, d_char, padding_idx=pad_id).to(self._device)

        # Embedding for speaker
        if d_speaker_emb is not None:
            self.speaker_in = nn.Linear(d_speaker_emb, d_speaker).to(self._device)

        jasper_params = jasper_kwargs['jasper']
        d_enc_out = jasper_params[-1]["filters"]
        self.jasper = nemo_asr.JasperEncoder(feat_in=d_char + int(d_speaker or 0), **jasper_kwargs).to(self._device)

        self.out = nn.Conv1d(d_enc_out, d_out, kernel_size=1, bias=True).to(self._device)

    def forward(self, text, text_mask, text_rep=None, text_rep_mask=None, speaker_emb=None):
        if text_rep is not None:
            text, text_mask = text_rep, text_rep_mask

        x = self.text_emb(text)  # BT => BTE
        x_len = text_mask.sum(-1)

        if speaker_emb is not None:
            speaker_x = self.speaker_in(speaker_emb)  # BZ => BS
            speaker_x = speaker_x.unsqueeze(1).repeat([1, x.shape[1], 1])  # BS => BTS
            x = torch.cat([x, speaker_x], dim=-1)  # stack([BTE, BTS]) = BT(E + S)

        pred, pred_len = self.jasper(x.transpose(-1, -2), x_len, force_pt=True)
        assert x.shape[1] == pred.shape[-1]  # Time
        assert torch.equal(x_len, pred_len)

        pred = self.out(pred).transpose(-1, -2)  # BTO

        return pred, pred_len


class FasterSpeechDurLoss(LossNM):
    """Neural Module Wrapper for Faster Speech Dur Loss."""

    @property
    @add_port_docs
    def input_ports(self):
        """Returns definitions of module input ports."""
        return dict(
            dur_true=NeuralType(('B', 'T'), LengthsType()),
            dur_pred=NeuralType(('B', 'T', 'D'), ChannelType()),
            text_mask=NeuralType(('B', 'T'), MaskType()),
        )

    @property
    @add_port_docs
    def output_ports(self):
        """Returns definitions of module output ports."""
        return dict(loss=NeuralType(None))

    def __init__(
        self, method='l2-log', num_classes=32, dmld_hidden=5, reduction='all', max_dur=500, xe_steps_coef=1.5,
    ):
        super().__init__()

        self._method = method
        self._num_classes = num_classes
        self._dmld_hidden = dmld_hidden
        self._reduction = reduction

        # Creates XE Steps classes.
        classes = np.arange(num_classes).tolist()
        k, c = 1, num_classes - 1
        while c < max_dur:
            k *= xe_steps_coef
            c += k
            classes.append(int(c))
        self._xe_steps_classes = classes
        if self._method == 'xe-steps':
            nemo.logging.info('XE Steps Classes: %s', str(classes))

    def _loss_function(self, dur_true, dur_pred, text_mask):
        if self._method.startswith('l2'):
            if dur_pred.shape[-1] != 1:
                raise ValueError("Wrong `dur_pred` shape.")
            dur_pred = dur_pred.squeeze(-1)

        if self._method == 'l2-log':
            loss = F.mse_loss(dur_pred, (dur_true + 1).float().log(), reduction='none')
        elif self._method == 'l2':
            loss = F.mse_loss(dur_pred, dur_true.float(), reduction='none')
        elif self._method == 'dmld-log':
            # [0, inf] => [0, num_classes - 1]
            dur_true = torch.clamp(dur_true, max=self._num_classes - 1)
            # [0, num_classes - 1] => [0, log(num_classes)]
            dur_true = (dur_true + 1).float().log()
            # [0, log(num_classes)] => [-1, 1]
            dur_true = (torch.clamp(dur_true / math.log(self._num_classes), max=1.0) - 0.5) * 2

            loss = nemo_tts.parts.dmld_loss(dur_pred, dur_true, self._num_classes)
        elif self._method == 'dmld':
            # [0, inf] => [0, num_classes - 1]
            dur_true = torch.clamp(dur_true, max=self._num_classes - 1)
            # [0, num_classes - 1] => [-1, 1]
            dur_true = (dur_true / (self._num_classes - 1) - 0.5) * 2

            loss = nemo_tts.parts.dmld_loss(dur_pred, dur_true, self._num_classes)
        elif self._method == 'xe':
            # [0, inf] => [0, num_classes - 1]
            dur_true = torch.clamp(dur_true, max=self._num_classes - 1)

            loss = F.cross_entropy(input=dur_pred.transpose(1, 2), target=dur_true, reduction='none')
        elif self._method == 'xe-steps':
            # [0, inf] => [0, xe-steps-num-classes - 1]
            classes = torch.tensor(self._xe_steps_classes, device=dur_pred.device)
            a = dur_true.unsqueeze(-1).repeat(1, 1, *classes.shape)
            b = classes.unsqueeze(0).unsqueeze(0).repeat(*dur_true.shape, 1)
            dur_true = (a - b).abs().argmin(-1)

            loss = F.cross_entropy(input=dur_pred.transpose(1, 2), target=dur_true, reduction='none')
        else:
            raise ValueError("Wrong Method")

        loss *= text_mask.float()
        if self._reduction == 'all':
            loss = loss.sum() / text_mask.sum()
        elif self._reduction == 'batch':
            loss = loss.sum(-1) / text_mask.sum(-1)
            loss = loss.mean()
        else:
            raise ValueError("Wrong Reduction")

        return loss

    @property
    def d_out(self):
        if self._method == 'l2-log':
            return 1
        elif self._method == 'l2':
            return 1
        elif self._method == 'dmld-log':
            return 3 * self._args.loss_dmld_hidden
        elif self._method == 'dmld':
            return 3 * self._args.loss_dmld_hidden
        elif self._method == 'xe':
            return self._num_classes
        elif self._method == 'xe-steps':
            # noinspection PyTypeChecker
            return len(self._xe_steps_classes)
        else:
            raise ValueError("Wrong Method")

    def preprocessing(self, tensors):
        if self._method == 'l2-log':
            dur_pred = tensors.dur_pred.squeeze(-1).exp() - 1
        elif self._method == 'l2':
            dur_pred = tensors.dur_pred.squeeze(-1)
        elif self._method == 'dmld-log':
            dur_pred = nemo_tts.parts.dmld_sample(tensors.dur_pred)

            # [-1, 1] => [0, log(num_classes)]
            dur_pred = (dur_pred + 1) / 2 * math.log(self._loss_dmld_num_classes)
            # [0, log(num_classes)] => [0, num_classes - 1]
            dur_pred = torch.clamp(dur_pred.exp() - 1, max=self._loss_dmld_num_classes - 1)
        elif self._method == 'dmld':
            dur_pred = nemo_tts.parts.dmld_sample(tensors.dur_pred)

            # [-1, 1] => [0, num_classes - 1]
            dur_pred = (dur_pred + 1) / 2 * (self._num_classes - 1)
        elif self._method == 'xe':
            dur_pred = tensors.dur_pred.argmax(-1)
        elif self._method == 'xe-steps':
            dur_pred = tensors.dur_pred.argmax(-1)
            classes = torch.tensor(self._xe_steps_classes, device=dur_pred.device)
            b = classes.unsqueeze(0).unsqueeze(0).repeat(*dur_pred.shape, 1)
            dur_pred = b.gather(-1, dur_pred.unsqueeze(-1)).squeeze(-1)
        else:
            raise ValueError("Wrong Method")

        dur_pred[dur_pred < 0.0] = 0.0
        dur_pred = dur_pred.float().round().long()
        tensors.dur_pred = dur_pred

        return tensors


class FasterSpeechMelLoss(LossNM):
    """Neural Module Wrapper for Faster Speech Mel Loss."""

    @property
    @add_port_docs
    def input_ports(self):
        """Returns definitions of module input ports."""
        return dict(
            mel_true=NeuralType(('B', 'D', 'T'), MelSpectrogramType()),
            mel_pred=NeuralType(('B', 'T', 'D'), ChannelType()),
            mel_len=NeuralType(('B',), LengthsType()),
            dur_true=NeuralType(('B', 'T'), LengthsType()),
            text_rep_mask=NeuralType(('B', 'T'), MaskType()),
        )

    @property
    @add_port_docs
    def output_ports(self):
        """Returns definitions of module output ports."""
        return dict(loss=NeuralType(None))

    def __init__(self, reduction='all'):
        super().__init__()

        self._reduction = reduction

    def _loss_function(self, mel_true, mel_pred, mel_len, dur_true, text_rep_mask):
        if not torch.equal(mel_len, dur_true.sum(-1)) or not torch.equal(mel_len, text_rep_mask.sum(-1)):
            raise ValueError("Wrong mel length calculation.")

        loss = F.mse_loss(mel_pred, mel_true.transpose(-1, -2), reduction='none').mean(-1)

        loss *= text_rep_mask.float()
        if self._reduction == 'all':
            loss = loss.sum() / text_rep_mask.sum()
        elif self._reduction == 'batch':
            loss = loss.sum(-1) / text_rep_mask.sum(-1)
            loss = loss.mean()
        else:
            raise ValueError("Wrong reduction.")

        return loss
