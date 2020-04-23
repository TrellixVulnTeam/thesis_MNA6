import torch
from models.resnet_simclr import ResNetSimCLR
from torch.utils.tensorboard import SummaryWriter
import torch.nn.functional as F
from loss.nt_xent import NTXentLoss
import os
import json
import sys
from util import find_run_name
from tqdm import tqdm


apex_support = False
try:
    sys.path.append('./apex')
    from apex import amp

    apex_support = True
except:
    print("Please install apex for mixed precision training from: https://github.com/NVIDIA/apex")
    apex_support = False

import numpy as np

torch.manual_seed(0)


def _save_config_file(model_checkpoints_folder, opt):
    if not os.path.exists(model_checkpoints_folder):
        os.makedirs(model_checkpoints_folder)
        with open('{}/commandline_args.txt'.format(model_checkpoints_folder), 'w') as f:
            json.dump(opt.__dict__, f, indent=2)


class SimCLR(object):

    def __init__(self, dataset, opt):
        self.opt = opt
        self.device = self._get_device()
        self.run_name = find_run_name(opt)
        self.writer = SummaryWriter("runs_simCLR_adap/run_{}".format(self.run_name))
        self.dataset = dataset
        self.nt_xent_criterion = NTXentLoss(self.device, opt)


    def _get_device(self):
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        print("Running on:", device)
        return device

    def _step(self, model, xis, xjs, n_iter):

        # get the representations and the projections
        ris, zis = model(xis)  # [N,C]
        # get the representations and the projections
        rjs, zjs = model(xjs)  # [N,C]

        # normalize projection feature vectors
        zis = F.normalize(zis, dim=1)
        zjs = F.normalize(zjs, dim=1)

        return zis, zjs
        # loss = self.nt_xent_criterion(zis, zjs)
        # return loss

    def train(self):

        train_loader, valid_loader = self.dataset.get_data_loaders()

        model = ResNetSimCLR(self.opt.base_model, self.opt.out_dim).to(self.device)

        model = self._load_pre_trained_weights(model)

        optimizer = torch.optim.Adam(model.parameters(), 3e-4, weight_decay=self.opt.weight_decay)

        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=len(train_loader), eta_min=0,
                                                               last_epoch=-1)

        if apex_support and self.opt.fp16_precision:
            model, optimizer = amp.initialize(model, optimizer,
                                              opt_level='O2',
                                              keep_batchnorm_fp32=True)

        model_checkpoints_folder = os.path.join(self.writer.log_dir, 'checkpoints')

        # save config file
        _save_config_file(model_checkpoints_folder, self.opt)

        n_iter = 0
        valid_n_iter = 0
        best_valid_loss = np.inf

        # create progress bar
        pbar = tqdm(total=self.opt.epochs)
        pbar.update(n_iter)

        tempi = []
        tempj = []
        for epoch_counter in range(self.opt.epochs):
            for x in train_loader:
                n_iter += 1
                xis = x[0]
                xjs = x[1]

                xis = xis.to(self.device)
                xjs = xjs.to(self.device)

                # loss = self._step(model, xis, xjs, n_iter)
                zis, zjs = self._step(model, xis, xjs, n_iter)
                tempi.append(zis)
                tempj.append(zjs)

                if n_iter % 4 == 0:
                    # create tensor from tempi and tempj
                    cat_zis = torch.cat(tempi, dim=0)
                    cat_zjs = torch.cat(tempj, dim=0)

                    loss = self.nt_xent_criterion(cat_zis, cat_zjs)

                    if apex_support and self.opt.fp16_precision:
                        with amp.scale_loss(loss, optimizer) as scaled_loss:
                            scaled_loss.backward()
                    else:
                        loss.backward()


                    # if n_iter % self.config['log_every_n_steps'] == 0:
                    self.writer.add_scalar('train_loss', loss, global_step=n_iter)


                    optimizer.step()
                    optimizer.zero_grad()
                    tempi = []
                    tempj = []

            pbar.update(1)

            # validate the model if requested
            if epoch_counter % self.opt.eval_every_n_epochs == 0:
                valid_loss = self._validate(model, valid_loader)
                if valid_loss < best_valid_loss:
                    # save the model weights
                    best_valid_loss = valid_loss
                    torch.save(model.state_dict(), os.path.join(model_checkpoints_folder, 'model.pth'))

                self.writer.add_scalar('validation_loss', valid_loss, global_step=valid_n_iter)
                valid_n_iter += 1

            # warmup for the first 10 epochs
            if epoch_counter >= 10:
                scheduler.step()
            self.writer.add_scalar('cosine_lr_decay', scheduler.get_lr()[0], global_step=n_iter)

    def _load_pre_trained_weights(self, model):
        try:
            checkpoints_folder = os.path.join('./runs_simCLR', self.opt.fine_tune_from, 'checkpoints')
            state_dict = torch.load(os.path.join(checkpoints_folder, 'model.pth'))
            model.load_state_dict(state_dict)
            print("Loaded pre-trained model with success.")
        except FileNotFoundError:
            print("Pre-trained weights not found. Training from scratch.")

        return model

    def _validate(self, model, valid_loader):
        # validation steps
        with torch.no_grad():
            model.eval()

            valid_loss = 0.0

            for counter, x in enumerate(valid_loader):
                xis = x[0]
                xjs = x[1]
                xis = xis.to(self.device)
                xjs = xjs.to(self.device)

                loss = self._step(model, xis, xjs, counter)
                valid_loss += loss.item()

            valid_loss /= counter
        model.train()
        return valid_loss