from abc import ABC

import torch
import pytorch_lightning as pl
import typing as th

from captum.attr import Saliency
import raster.models as models
from raster.utils import KeyValue, boolify
from l5kit.configs import load_config_data

from .saliency_supervision import SaliencySupervision
from .utils import find_batch_extremes, filter_batch, neg_multi_log_likelihood
from argparse import ArgumentParser


class LyftTrainerModule(pl.LightningModule, ABC):
    def __init__(
            self,
            config: dict,
            model: str = 'Resnet',
            model_dict: dict = None,
            modes: int = 1,
            optimizer: str = 'Adam',
            optimizer_dict: th.Optional[dict] = None,
            lr: float = 1e-4,
            scheduler: th.Optional[str] = None,
            scheduler_dict: th.Optional[dict] = None,
            scheduler_interval: str = 'epoch',
            scheduler_frequency: int = 1,
            scheduler_monitor: th.Optional[str] = None,
            saliency_factor: float = 0.,
            saliency_intrest: str = 'simple',
            saliency_dict: th.Optional[dict] = None,
            pgd_mode: str = 'loss',
            pgd_reg_factor: float = 0.,
            pgd_iters: int = 0,
            pgd_alpha: float = 0.01,
            pgd_random_start: bool = False,
            pgd_eps_vehicles: float = 0.4,
            pgd_eps_semantics: float = 0.15625,
            track_grad: bool = False,
            **kwargs,
    ):
        super().__init__()
        self.save_hyperparameters()

        # initializing model
        self.model = getattr(models, self.hparams.model)(config=self.hparams.config, modes=self.hparams.modes,
                                                         **(self.hparams.model_dict or dict()))
        # saliency
        self.saliency = SaliencySupervision(self.hparams.saliency_intrest, **(self.hparams.saliency_dict or dict())
                                            ) if self.hparams.saliency_factor else None
        # optimization & scheduling
        self.lr = self.hparams.lr
        self.track_grad = track_grad

    def pgd_attack(self, inputs, outputs, target_availabilities=None, return_loss=True):
        targets = outputs
        if self.hparams.pgd_random_start or self.hparams.pgd_mode == 'negative_sample':
            delta = (torch.rand_like(inputs) - 0.5) * 2
            for (s_channel, end_channel), eps in [
                ((- 3, None), self.hparams.pgd_eps_semantics),
                ((0, - 3), self.hparams.pgd_eps_vehicles)
            ]:
                delta[:, s_channel:end_channel] *= eps
            delta.requires_grad = True
        else:
            delta = torch.zeros_like(inputs, requires_grad=True)

        # freezing model params
        for param in self.parameters():
            param.requires_grad = False
        self.eval()
        if self.hparams.pgd_mode == 'negative_sample':
            targets, _ = self.model(inputs.detach())
        for i in range(self.hparams.pgd_iters):
            loss = neg_multi_log_likelihood(targets, *self.model((inputs.detach() + delta).clamp(0, 1.)),
                                            target_availabilities).mean()
            if i == 0 and return_loss:
                init_loss = loss.detach()
            loss.backward()
            delta.data = delta.data + self.hparams.pgd_alpha * delta.grad.detach().sign()
            for (s_channel, end_channel), eps in [
                ((- 3, None), self.hparams.pgd_eps_semantics),
                ((0, - 3), self.hparams.pgd_eps_vehicles)
            ]:
                delta.data[:, s_channel:end_channel].clamp_(-eps, eps)
            delta.grad.zero_()
        if return_loss:
            final_loss = neg_multi_log_likelihood(targets, *self.model((inputs + delta).detach().clamp(0, 1.)),
                                                  target_availabilities).mean()

        # unfreezing model
        for param in self.parameters():
            param.requires_grad = True
        self.train()
        if return_loss:
            return (inputs.detach() + delta.detach()).clamp(0, 1.), init_loss, final_loss
        return (inputs.detach() + delta.detach()).clamp(0, 1.)

    def forward(self, inputs, targets: torch.Tensor, target_availabilities: torch.Tensor = None, return_results=True,
                grad_enabled=True, attack=True):
        torch.set_grad_enabled(grad_enabled)
        res = dict()
        perf_attack = self.hparams.pgd_iters and attack
        reg_pgd = perf_attack and self.hparams.pgd_reg_factor
        if perf_attack:
            adv_inputs, init_loss, final_loss = self.pgd_attack(
                inputs, targets, target_availabilities, return_loss=True)
            if not self.hparams.pgd_reg_factor:
                inputs = adv_inputs
            res['adv/init_loss'] = init_loss
            res['adv/final_loss'] = final_loss
        inputs.requires_grad = bool(self.hparams.saliency_factor) or self.track_grad
        pred, conf = self.model(inputs)
        nll = neg_multi_log_likelihood(targets, pred, conf, target_availabilities)
        if (self.hparams.saliency_factor or self.track_grad) and grad_enabled:
            grads = torch.autograd.grad(nll.unbind(), inputs, create_graph=bool(self.hparams.saliency_factor))[0]
            res['grads/semantics'] = grads.data[:, -3:].abs().sum()
            res['grads/vehicles'] = grads.data[:, :-3].abs().sum()
            res['grads/total'] = res['grads/semantics'] + res['grads/vehicles']
        res['nll'] = nll.mean()
        if reg_pgd:
            adv_nll = neg_multi_log_likelihood(pred.detach() if self.hparams.pgd_mode == 'negative_sample' else targets,
                                               *self.model(adv_inputs), target_availabilities)
            res['adv/nll'] = adv_nll.mean()
        if self.hparams.saliency_factor and grad_enabled:
            sal_res = self.saliency(grads)
            if not reg_pgd:
                res['loss'] = ((1 - sal_res) * self.hparams.saliency_factor * nll.detach() + nll).mean()
            else:
                res['loss'] = ((1 - sal_res) * self.hparams.saliency_factor * nll.detach() + nll
                               + self.hparams.pgd_reg_factor * adv_nll).mean()
            res['saliency'] = sal_res.mean()
        else:
            res['loss'] = nll.mean() if not reg_pgd else (nll + self.hparams.pgd_reg_factor * adv_nll).mean()
        if return_results:
            return res
        return res['loss']

    def step(self, batch, batch_idx, optimizer_idx=None, name='train'):
        is_val = name == 'val'
        result = self(batch['image'], batch['target_positions'], batch.get('target_availabilities', None),
                      return_results=True, attack=not is_val)
        for item, value in result.items():
            self.log(f'{item}/{name}', value.mean(), on_step=not is_val, on_epoch=is_val, logger=True, sync_dist=True)
        if not is_val:
            return result['loss']

    def training_step(self, batch, batch_idx, optimizer_idx=None):
        return self.step(batch, batch_idx, optimizer_idx, name='train')

    def validation_step(self, batch, batch_idx):
        return self.step(batch, batch_idx, name='val')

    def configure_optimizers(self):
        opt_class, opt_dict = torch.optim.Adam, {'lr': self.lr}
        if self.hparams.optimizer:
            opt_class = getattr(torch.optim, self.hparams.optimizer)
            opt_dict = self.hparams.optimizer_dict or dict()
            opt_dict['lr'] = self.lr
        opt = opt_class(self.parameters(), **opt_dict)
        if self.hparams.scheduler is None:
            return opt
        sched_class = getattr(torch.optim.lr_scheduler, self.hparams.scheduler)
        sched_dict = self.hparams.scheduler_dict or dict()
        sched = sched_class(opt, **sched_dict)
        sched_instance_dict = dict(
            scheduler=sched, interval=self.hparams.scheduler_interval, frequency=self.hparams.scheduler_frequency,
            reduce_on_plateau=isinstance(sched, torch.optim.lr_scheduler.ReduceLROnPlateau)
        )
        if self.hparams.scheduler_monitor:
            sched_instance_dict['monitor'] = self.hparams.scheduler_monitor
        return [opt], [sched_instance_dict]

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument('--model', type=str, default='Resnet', help='model architecture class to use')
        parser.add_argument('--model-dict', nargs='*', default=dict(), action=KeyValue,
                            help='additional model specific args')
        parser.add_argument('--modes', type=int, default=1, help='number of modes of model prediction')
        parser.add_argument('--optimizer', type=str, default='Adam', help='optimizer to use')
        parser.add_argument('--optimizer-dict', nargs='*', action=KeyValue, help='additional optimizer specific args')
        parser.add_argument('--lr', type=float, default=1e-4, help='learning rate')
        parser.add_argument('--scheduler', type=str, default=None, help='scheduler to use')
        parser.add_argument('--scheduler-dict', nargs='*', action=KeyValue, help='additional scheduler specific args')
        parser.add_argument('--scheduler-interval', type=str, default='epoch',
                            help='interval to call scheduler.step [step/epoch]')
        parser.add_argument('--scheduler-frequency', type=int, default=1,
                            help='frequency of calling scheduler.step on each interval')
        parser.add_argument('--scheduler-monitor', type=str, default=None,
                            help='metric to monitor for scheduling process')
        parser.add_argument('--pgd-mode', type=str, default='loss', help='pgd attack mode [loss/negative_sample]')
        parser.add_argument('--pgd-reg-factor', type=float, default=0,
                            help='whether to use adversaries as a regularization term')
        parser.add_argument('--pgd-iters', type=int, default=0, help='pgd attack number of iterations')
        parser.add_argument('--pgd-random-start', type=boolify, default=False,
                            help='whether to use a random point for adversary search during pgd attack')
        parser.add_argument('--pgd-alpha', type=float, default=1e-2, help='pgd attack alpha value')
        parser.add_argument('--pgd-eps-vehicles', type=float, default=0.4,
                            help='epsilon bound for pgd attack on vehicle layers')
        parser.add_argument('--pgd-eps-semantics', type=float, default=0.15625,
                            help='epsilon bound for pgd attack on semantic layers')
        parser.add_argument('--saliency-factor', type=float, default=0., help='saliency supervision factor')
        parser.add_argument('--saliency-intrest', type=str, default='simple',
                            help='intrest region calculation for saliency supervision')
        parser.add_argument('--saliency-dict', nargs='*', action=KeyValue,
                            help='additional saliency supervision specific args')
        parser.add_argument('--track-grad', type=boolify, default=False,
                            help='whether to log grad norms')
        return parser
