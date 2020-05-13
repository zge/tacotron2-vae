import random
import numpy as np
import torch
import torch.utils.data
import os

import layers
from utils import load_wav_to_torch, load_filepaths_and_text
from text import text_to_sequence


class TextMelLoader(torch.utils.data.Dataset):
    """
        1) loads audio,text pairs
        2) normalizes text and converts them to sequences of one-hot vectors
        3) computes mel-spectrograms from audio files.
    """
    def __init__(self, audiopaths_and_text, hparams):
        self.audiopaths_and_text = load_filepaths_and_text(audiopaths_and_text)
        self.include_emo_emb = hparams.include_emo_emb
        self.emo_emb_dim = hparams.emo_emb_dim
        self.text_cleaners = hparams.text_cleaners
        self.max_wav_value = hparams.max_wav_value
        self.sampling_rate = hparams.sampling_rate
        self.load_mel_from_disk = hparams.load_mel_from_disk
        self.n_speakers = hparams.n_speakers
        self.n_emotions = hparams.n_emotions
        self.stft = layers.TacotronSTFT(
            hparams.filter_length, hparams.hop_length, hparams.win_length,
            hparams.n_mel_channels, hparams.sampling_rate, hparams.mel_fmin,
            hparams.mel_fmax)
        random.seed(hparams.seed)
        random.shuffle(self.audiopaths_and_text)

    def get_mel_text_pair(self, audiopath_and_text):
        # separate filename and text
        if self.include_emo_emb:
          audiopath, emoembpath, text, speaker, emotion = audiopath_and_text
          emoemb = self.get_emoemb(emoembpath)
        else:
          audiopath, text, speaker, emotion = audiopath_and_text # filelists/*.txt
          emoemb = ''
        text = self.get_text(text) # int_tensor[char_index, ....]
        mel = self.get_mel(audiopath) # []
        speaker = self.get_speaker(speaker) # currently single speaker
        emotion = self.get_emotion(emotion)

        audioid = os.path.splitext(os.path.basename(audiopath))[0]

        return (text, mel, emoemb, speaker, emotion, audioid)

    def get_mel(self, filename):
        if not self.load_mel_from_disk:
            audio, sampling_rate = load_wav_to_torch(filename)
            if sampling_rate != self.stft.sampling_rate:
                raise ValueError("{} SR doesn't match target {} SR".format(
                    sampling_rate, self.stft.sampling_rate))
            audio_norm = audio / self.max_wav_value
            audio_norm = audio_norm.unsqueeze(0)
            audio_norm = torch.autograd.Variable(audio_norm, requires_grad=False)
            melspec = self.stft.mel_spectrogram(audio_norm)
            melspec = torch.squeeze(melspec, 0)
        else:
            melspec = torch.from_numpy(np.load(filename))
            assert melspec.size(0) == self.stft.n_mel_channels, (
                'Mel dimension mismatch: given {}, expected {}'.format(
                    melspec.size(0), self.stft.n_mel_channels))

        return melspec

    def get_emoemb(self, filename):
        emoemb = torch.from_numpy(np.load(filename)).T
        assert emoemb.size(0) == self.emo_emb_dim, (
            'Emotion embedding dimension mismatch: given {}, expected {}'.format(
                emoemb.size(0), self.emo_emb_dim))
        return emoemb

    def get_text(self, text):
        text_norm = torch.IntTensor(text_to_sequence(text, self.text_cleaners))
        return text_norm

    def get_speaker(self, speaker):
        speaker_vector = np.zeros(self.n_speakers)
        speaker_vector[int(speaker)] = 1
        return torch.Tensor(speaker_vector.astype(dtype=np.float32))

    def get_emotion(self, emotion):
        emotion_vector = np.zeros(self.n_emotions)
        emotion_vector[int(emotion)] = 1
        return torch.Tensor(emotion_vector.astype(dtype=np.float32))

    def __getitem__(self, index):
        return self.get_mel_text_pair(self.audiopaths_and_text[index])

    def __len__(self):
        return len(self.audiopaths_and_text)


class TextMelCollate():
    """ Zero-pads model inputs and targets based on number of frames per setep
    """
    def __init__(self, n_frames_per_step):
        self.n_frames_per_step = n_frames_per_step

    def __call__(self, batch):
        """Collate's training batch from normalized text and mel-spectrogram
        PARAMS
        ------
        batch: [[text_normalized, mel_normalized], ...]
        """
        # Right zero-pad all one-hot text sequences to max input length
        input_lengths, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([len(x[0]) for x in batch]),
            dim=0, descending=True)
        max_input_len = input_lengths[0]

        text_padded = torch.LongTensor(len(batch), max_input_len)
        text_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            text = batch[ids_sorted_decreasing[i]][0]
            text_padded[i, :text.size(0)] = text

        speakers = torch.LongTensor(len(batch), len(batch[0][3]))
        for i in range(len(ids_sorted_decreasing)):
            speaker = batch[ids_sorted_decreasing[i]][3]
            speakers[i, :] = speaker

        emotions = torch.LongTensor(len(batch), len(batch[0][4]))
        for i in range(len(ids_sorted_decreasing)):
            emotion = batch[ids_sorted_decreasing[i]][4]
            emotions[i, :] = emotion

        audioids = [[] for _ in range(len(batch))]
        for i in range(len(ids_sorted_decreasing)):
            audioids[i] = batch[ids_sorted_decreasing[i]][5]

        # Right zero-pad mel-spec
        num_mels = batch[0][1].size(0)
        max_target_len1 = max([x[1].size(1) for x in batch])

        if len(batch[0][2]) > 0:
          num_emoembs = batch[0][2].size(0)
          max_target_len2 = max([x[2].size(1) for x in batch])

        max_target_len = max_target_len1 # temp solution
        # todo: uniform wintime/hoptime of mel and emoemb so max_target_len will be the same

        # max_target_len = min(max([x[1].size(1) for x in batch]), 1000) # max_len 1000
        # increment max_target_len to the multiples of n_frames_per_step
        if max_target_len % self.n_frames_per_step != 0:
            max_target_len += self.n_frames_per_step - max_target_len % self.n_frames_per_step
            assert max_target_len % self.n_frames_per_step == 0
            # todo: to support n_frames_per_step > 1

        # include mel padded and gate padded
        mel_padded = torch.FloatTensor(len(batch), num_mels, max_target_len)
        mel_padded.zero_()
        gate_padded = torch.FloatTensor(len(batch), max_target_len)
        gate_padded.zero_()
        output_lengths = torch.LongTensor(len(batch))
        for i in range(len(ids_sorted_decreasing)):
            mel = batch[ids_sorted_decreasing[i]][1]
            mel_padded[i, :, :mel.size(1)] = mel
            gate_padded[i, mel.size(1)-1:] = 1
            output_lengths[i] = mel.size(1)

        if len(batch[0][2]) > 0:
            emoemb_padded = torch.FloatTensor(len(batch), num_emoembs, max_target_len)
            emoemb_padded.zero_()
            for i in range(len(ids_sorted_decreasing)):
                emoemb = batch[ids_sorted_decreasing[i]][2]
                emoemb_nframes = min(emoemb.size(1), max_target_len)  # temp solution
                emoemb_padded[i, :, :emoemb_nframes] = emoemb[:, :emoemb_nframes]
        else:
            emoemb_padded = ''

        return text_padded, input_lengths, mel_padded, emoemb_padded, \
            gate_padded, output_lengths, speakers, emotions, audioids
