import os
import sys
import time
import argparse
import math
from numpy import finfo
import imageio
sys.path.append(os.getcwd())

import torch
from distributed import apply_gradient_allreduce
import torch.distributed as dist
from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import DataLoader

from model import Tacotron2
from data_utils import TextMelLoader, TextMelCollate
from plotting_utils import plot_scatter, plot_tsne, plot_kl_weight
from utils import get_kl_weight, get_text_padding_rate, get_mel_padding_rate
from utils import dict2col, dict2row
from loss_function import Tacotron2Loss_VAE, Tacotron2Loss
from logger import Tacotron2Logger

from hparams import create_hparams, hparams_debug_string # for LJSpeech
#from hparams_soe import create_hparams, hparams_debug_string # for SOE


def reduce_tensor(tensor, n_gpus):
    rt = tensor.clone()
    dist.all_reduce(rt, op=dist.reduce_op.SUM)
    rt /= n_gpus
    return rt


def init_distributed(hparams, n_gpus, rank, group_name):
    assert torch.cuda.is_available(), "Distributed mode requires CUDA."
    print("Initializing Distributed")

    # Set cuda device so everything is done on the right GPU.
    torch.cuda.set_device(rank % torch.cuda.device_count())

    # Initialize distributed communication
    dist.init_process_group(
        backend=hparams.dist_backend, init_method=hparams.dist_url,
        world_size=n_gpus, rank=rank, group_name=group_name)

    print("Done initializing distributed")


def prepare_dataloaders(hparams, epoch=0, valset=None, collate_fn=None):
    # Get data, data loaders and collate function ready
    shuffle_train = {'audiopath': hparams.shuffle_audiopaths,
        'batch': hparams.shuffle_batches, 'permute-opt': hparams.permute_opt}
    trainset = TextMelLoader(hparams.training_files, shuffle_train,
                             hparams, epoch)
    if valset is None:
        # valset has different shuffle plan compared with train set
        shuffle_val = {'audiopath': hparams.shuffle_audiopaths,
                       'batch': False, 'permute-opt': 'rand'}
        valset = TextMelLoader(hparams.validation_files, shuffle_val, hparams)
    if collate_fn is None:
        collate_fn = TextMelCollate(hparams)

    if hparams.distributed_run:
        train_sampler = DistributedSampler(trainset, shuffle=hparams.shuffle_samples)
    else:
        train_sampler = None

    shuffle = (train_sampler is None) and hparams.shuffle_samples
    train_loader = DataLoader(trainset, num_workers=1, shuffle=shuffle,
                              sampler=train_sampler,
                              batch_size=hparams.batch_size, pin_memory=False,
                              drop_last=True, collate_fn=collate_fn)
    return train_loader, valset, collate_fn


def prepare_directories_and_logger(output_directory, log_directory, rank,
                                   use_vae=False):
    if rank == 0:
        if not os.path.isdir(output_directory):
            os.makedirs(output_directory)
            os.chmod(output_directory, 0o775)
        logger = Tacotron2Logger(os.path.join(output_directory, log_directory),
                                 use_vae)
    else:
        logger = None
    return logger


def load_model(hparams):
    model = Tacotron2(hparams).cuda()
    if hparams.fp16_run:
        model.decoder.attention_layer.score_mask_value = finfo('float16').min

    if hparams.distributed_run:
        model = apply_gradient_allreduce(model)

    return model


def warm_start_model(checkpoint_path, model, ignore_layers):
    assert os.path.isfile(checkpoint_path)
    print("Warm starting model from checkpoint '{}'".format(checkpoint_path))
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')
    model_dict = checkpoint_dict['state_dict']
    if len(ignore_layers) > 0:
        model_dict = {k: v for k, v in model_dict.items()
                      if k not in ignore_layers}
        dummy_dict = model.state_dict()
        dummy_dict.update(model_dict)
        model_dict = dummy_dict
    model.load_state_dict(model_dict)
    return model


def load_checkpoint(checkpoint_path, model, optimizer):
    assert os.path.isfile(checkpoint_path)
    print("Loading checkpoint '{}'".format(checkpoint_path))
    checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')
    model.load_state_dict(checkpoint_dict['state_dict'])
    optimizer.load_state_dict(checkpoint_dict['optimizer'])
    learning_rate = checkpoint_dict['learning_rate']
    iteration = checkpoint_dict['iteration']
    print("Loaded checkpoint '{}' from iteration {}" .format(
        checkpoint_path, iteration))
    return model, optimizer, learning_rate, iteration


def save_checkpoint(model, optimizer, learning_rate, iteration, filepath):
    print("Saving model and optimizer state at iteration {} to {}".format(
        iteration, filepath))
    torch.save({'iteration': iteration,
                'state_dict': model.state_dict(),
                'optimizer': optimizer.state_dict(),
                'learning_rate': learning_rate}, filepath)


def track_seq(track, input_lengths, gate_padded, metadata, verbose=False):
    padding_rate_txt, max_len_txt, top_len_txt = get_text_padding_rate(input_lengths)
    padding_rate_mel, max_len_mel, top_len_mel = get_mel_padding_rate(gate_padded)
    duration, epoch, step = metadata
    if verbose:
        print('[{0}:{1}] dur: {2:.2f}, '.format(epoch, step, duration), end='')
        print('text (pad%: {0:.2f}%, top3: {1}), '.format(
          padding_rate_txt*100, top_len_txt), end='')
        print('mel (pad%: {0:.2f}%, top3: {1})'.format(
          padding_rate_mel*100, top_len_mel))
    track['padding-rate-txt'].append(padding_rate_txt)
    track['max-len-txt'].append(max_len_txt)
    track['top-len-txt'].append(top_len_txt)
    track['padding-rate-mel'].append(padding_rate_mel)
    track['max-len-mel'].append(max_len_mel)
    track['top-len-mel'].append(top_len_mel)
    track['duration'].append(duration)
    track['epoch'].append(epoch)
    track['step'].append(step)

def validate(model, criterion, valset, iteration, batch_size, n_gpus,
             collate_fn, logger, distributed_run, rank, use_vae=False):
    """Handles all the validation scoring and printing"""
    model.eval()
    with torch.no_grad():
        val_sampler = DistributedSampler(valset) if distributed_run else None
        val_loader = DataLoader(valset, sampler=val_sampler, num_workers=1,
                                shuffle=False, batch_size=batch_size,
                                pin_memory=False, collate_fn=collate_fn)

        val_loss = 0.0
        y0, y_pred0 = '', ''
        for i, batch in enumerate(val_loader):
            x, y = model.parse_batch(batch)
            y_pred = model(x)
            # save first batch (with full batch size) for logging later
            if not y0 and not y_pred0:
              y0, y_pred0 = y, y_pred
            if use_vae:
                loss, _, _, _ = criterion(y_pred, y, iteration)
            else:
                loss = criterion(y_pred, y)
            if distributed_run:
                reduced_val_loss = reduce_tensor(loss.data, n_gpus).item()
            else:
                reduced_val_loss = loss.item()
            val_loss += reduced_val_loss
        val_loss = val_loss / (i + 1)

    model.train()
    if rank == 0:
        print("Validation loss {}: {:9f}  ".format(iteration, val_loss))
        logger.log_validation(val_loss, model, y0, y_pred0, iteration)

    if use_vae:
        mus, emotions = y_pred0[4], y_pred0[7]
    else:
        mus, emotions = '', ''

    return val_loss, (mus, emotions)


def train(output_directory, log_directory, checkpoint_path, warm_start, n_gpus,
          rank, group_name, hparams):
    """Training and validation logging results to tensorboard and stdout

    Params
    ------
    output_directory (string): directory to save checkpoints
    log_directory (string) directory to save tensorboard logs
    checkpoint_path(string): checkpoint path
    n_gpus (int): number of gpus
    rank (int): rank of current gpu
    hparams (object): comma separated list of "name=value" pairs.
    """

    if hparams.distributed_run:
        init_distributed(hparams, n_gpus, rank, group_name)

    torch.manual_seed(hparams.seed)
    torch.cuda.manual_seed(hparams.seed)

    model = load_model(hparams)
    learning_rate = hparams.learning_rate
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate,
                                 weight_decay=hparams.weight_decay)

    if hparams.fp16_run:
        from apex import amp
        model, optimizer = amp.initialize(
            model, optimizer, opt_level='O2')

    if hparams.distributed_run:
        model = apply_gradient_allreduce(model)

    if hparams.use_vae:
        criterion = Tacotron2Loss_VAE(hparams)
    else:
        criterion = Tacotron2Loss()

    logger = prepare_directories_and_logger(
        output_directory, log_directory, rank, hparams.use_vae)

    train_loader, valset, collate_fn = prepare_dataloaders(hparams)

    # Load checkpoint if one exists
    iteration = 0
    epoch_offset = 0
    if checkpoint_path is not None:
        if warm_start:
            model = warm_start_model(
                checkpoint_path, model, hparams.ignore_layers)
        else:
            model, optimizer, _learning_rate, iteration = load_checkpoint(
                checkpoint_path, model, optimizer)
            if hparams.use_saved_learning_rate:
                learning_rate = _learning_rate
            iteration += 1  # next iteration is iteration + 1
            epoch_offset = max(0, int(iteration / len(train_loader)))
        print('completing loading model ...')

    model.train()
    is_overflow = False
    # ================ MAIN TRAINNIG LOOP! ===================
    track = {'padding-rate-txt':[], 'max-len-txt':[], 'top-len-txt':[],
             'padding-rate-mel':[], 'max-len-mel':[], 'top-len-mel':[],
             'duration': [], 'epoch': [], 'step': []}
    csvfile = os.path.join(output_directory, log_directory, 'track.csv')
    print('starting training in epoch range {} ~ {} ...'.format(
        epoch_offset, hparams.epochs))

    for epoch in range(epoch_offset, hparams.epochs):
        #if epoch >= 10: break
        print("Epoch: {}, #batches: {}".format(epoch, len(train_loader)))
        for i, batch in enumerate(train_loader):
            start = time.perf_counter()
            for param_group in optimizer.param_groups:
               param_group['lr'] = learning_rate

            model.zero_grad()
            x, y = model.parse_batch(batch)
            y_pred = model(x)

            if hparams.use_vae:
                loss, recon_loss, kl, kl_weight = criterion(y_pred, y, iteration)
            else:
                loss = criterion(y_pred, y)

            if hparams.distributed_run:
                reduced_loss = reduce_tensor(loss.data, n_gpus).item()
            else:
                reduced_loss = loss.item()

            if hparams.fp16_run:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            if hparams.fp16_run:
                grad_norm = torch.nn.utils.clip_grad_norm_(
                    amp.master_params(optimizer), hparams.grad_clip_thresh)
                is_overflow = math.isnan(grad_norm)
            else:
                grad_norm = torch.nn.utils.clip_grad_norm_(
                    model.parameters(), hparams.grad_clip_thresh)

            optimizer.step()

            if not is_overflow and rank == 0:
                duration = time.perf_counter() - start
                print("Train loss {} {:.6f} Grad Norm {:.6f} {:.2f}s/it".format(
                    iteration, reduced_loss, grad_norm, duration))
                input_lengths, gate_padded = batch[1], batch[4]
                metadata = (duration, epoch, i)
                track_seq(track, input_lengths, gate_padded, metadata)
                padding_rate_txt = track['padding-rate-txt'][-1]
                max_len_txt = track['max-len-txt'][-1]
                padding_rate_mel = track['padding-rate-mel'][-1]
                max_len_mel = track['max-len-mel'][-1]
                if hparams.use_vae:
                    logger.log_training(
                        reduced_loss, grad_norm, learning_rate, duration,
                        padding_rate_txt, max_len_txt, padding_rate_mel,
                        max_len_mel, iteration, recon_loss, kl, kl_weight)
                else:
                    logger.log_training(
                        reduced_loss, grad_norm, learning_rate, duration,
                        padding_rate_txt, max_len_txt, padding_rate_mel,
                        max_len_mel, iteration)

            if not is_overflow and (iteration % hparams.iters_per_checkpoint == 0):
                dict2col(track, csvfile, verbose=True)
                val_loss, (mus, emotions) = validate(model, criterion, valset,
                     iteration, hparams.batch_size, n_gpus, collate_fn, logger,
                     hparams.distributed_run, rank, hparams.use_vae)
                if rank == 0:
                    checkpoint_path = os.path.join(output_directory,
                        "checkpoint_{0}_{1:.4f}".format(iteration, val_loss))
                    save_checkpoint(model, optimizer, learning_rate, iteration,
                         checkpoint_path)
                    if hparams.use_vae:
                        image_scatter_path = os.path.join(output_directory,
                             "checkpoint_{0}_scatter_val.png".format(iteration))
                        image_tsne_path = os.path.join(output_directory,
                             "checkpoint_{0}_tsne_val.png".format(iteration))
                        imageio.imwrite(image_scatter_path, plot_scatter(mus, emotions))
                        imageio.imwrite(image_tsne_path, plot_tsne(mus, emotions))

            iteration += 1

        if hparams.prep_trainset_per_epoch:
            print('preparing train loader for epoch {}'.format(epoch+1))
            train_loader = prepare_dataloaders(hparams, epoch+1, valset, collate_fn)[0]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-o', '--output_directory', type=str,
                        help='directory to save checkpoints')
    parser.add_argument('-l', '--log_directory', type=str,
                        help='directory to save tensorboard logs')
    parser.add_argument('-c', '--checkpoint_path', type=str, default=None,
                        required=False, help='checkpoint path')
    parser.add_argument('--warm_start', action='store_true',
                        help='load model weights only, ignore specified layers')
    parser.add_argument('--n_gpus', type=int, default=1,
                        required=False, help='number of gpus')
    parser.add_argument('--rank', type=int, default=0,
                        required=False, help='rank of current gpu')
    parser.add_argument('--gpu', type=int, default=0,
                        required=False, help='current gpu device id')
    parser.add_argument('--group_name', type=str, default='group_name',
                        required=False, help='Distributed group name')
    parser.add_argument('--hparams', type=str,
                        required=False, help='comma separated name=value pairs')
    return parser.parse_args()


def log_args(args, argnames, logfile):
    dct = {name: eval('args.{}'.format(name)) for name in argnames}
    dict2row(dct, logfile, order='ascend')


if __name__ == '__main__':

    # # runtime mode
    # args = parse_args()

    # interactive mode
    args = argparse.ArgumentParser()
    args.output_directory = 'outdir/ljspeech/semi-sorted2'
    args.log_directory = 'logdir'
    args.checkpoint_path = None # fresh run
    args.warm_start = False
    args.n_gpus = 1
    args.rank = 0
    args.gpu = 0
    args.group_name = 'group_name'
    hparams = ["training_files=filelists/ljspeech/ljspeech_wav_train.txt",
               "validation_files=filelists/ljspeech/ljspeech_wav_valid.txt",
               "filelist_cols=[audiopath,text,dur,speaker,emotion]",
               "shuffle_audiopaths=True",
               "shuffle_batches=True",
               "shuffle_samples=False",
               "permute_opt=semi-sort",
               "local_rand_factor=0.1",
               "prep_trainset_per_epoch=True",
               "override_sample_size=False",
               "text_cleaners=[english_cleaners]",
               "use_vae=False",
               "anneal_function=logistic",
               "use_saved_learning_rate=True",
               "load_mel_from_disk=False",
               "include_emo_emb=False",
               "vae_input_type=mel",
               "fp16_run=False",
               "embedding_variation=0",
               "label_type=one-hot",
               "distributed_run=False",
               "batch_size=16",
               "iters_per_checkpoint=2000",
               "anneal_x0=100000",
               "anneal_k=0.0001"]
    args.hparams = ','.join(hparams)

    # create log directory due to saving files before training starts
    if not os.path.isdir(args.output_directory):
      print('creating dir: {} ...'.format(args.output_directory))
      os.makedirs(args.output_directory)
      os.chmod(args.output_directory, 0o775)

    argnames = ['output_directory', 'log_directory', 'checkpoint_path',
                'warm_start', 'n_gpus', 'rank', 'gpu', 'group_name']
    args_csv = os.path.join(args.output_directory, 'args.csv')
    log_args(args, argnames, args_csv)

    hparams = create_hparams(args.hparams)
    hparams_csv = os.path.join(args.output_directory, 'hparams.csv')
    print(hparams_debug_string(hparams, hparams_csv))

    if args.n_gpus == 1:
        # set current GPU device
        torch.cuda.set_device(args.gpu)
    print('current GPU: {}'.format(torch.cuda.current_device()))

    torch.backends.cudnn.enabled = hparams.cudnn_enabled
    torch.backends.cudnn.benchmark = hparams.cudnn_benchmark

    print("shuffle audiopaths:", hparams.shuffle_audiopaths)
    print("permute option:", hparams.permute_opt)
    print("local_rand_factor:", hparams.local_rand_factor)
    print("prep trainset per epoch:", hparams.prep_trainset_per_epoch)
    print("FP16 Run:", hparams.fp16_run)
    print("Dynamic Loss Scaling:", hparams.dynamic_loss_scaling)
    print("Distributed Run:", hparams.distributed_run)
    print("Override Sample Size:", hparams.override_sample_size)
    print("Load Mel from Disk:", hparams.load_mel_from_disk)
    print("Use VAE:", hparams.use_vae)
    print("Include Emotion Embedding:", hparams.include_emo_emb)
    print("Label Type:", hparams.label_type)
    print("VAE Input Type:", hparams.vae_input_type)
    print("Embedding Variation:", hparams.embedding_variation)
    print("cuDNN Enabled:", hparams.cudnn_enabled)
    print("cuDNN Benchmark:", hparams.cudnn_benchmark)

    # log kl weights
    if hparams.use_vae:
        af = hparams.anneal_function
        lag = hparams.anneal_lag
        k = hparams.anneal_k
        x0 = hparams.anneal_x0
        upper = hparams.anneal_upper
        constant = hparams.anneal_constant
        kl_weights = get_kl_weight(af, lag, k, x0, upper, constant, nsteps=250000)
        imageio.imwrite(os.path.join(args.output_directory, 'kl_weights.png'),
                        plot_kl_weight(kl_weights, af, lag,k, x0, upper, constant))

    output_directory = args.output_directory
    log_directory = args.log_directory
    checkpoint_path = args.checkpoint_path
    warm_start = args.warm_start
    n_gpus = args.n_gpus
    rank = args.rank
    group_name = args.group_name

    train(output_directory, log_directory, checkpoint_path,
          warm_start, n_gpus, rank, group_name, hparams)
