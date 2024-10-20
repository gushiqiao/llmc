from abc import ABCMeta, abstractmethod

import torch
from loguru import logger


class BlockwiseOpt(metaclass=ABCMeta):
    def __init__(self, model, quant_config, input, padding_mask, config):
        self.model = model
        self.blocks = model.get_blocks()
        self.quant_config = quant_config
        self.sparsity_config = quant_config
        self.input = input
        self.padding_mask = padding_mask
        self.data_free = False if self.input else True
        self.config = config
        self.block_idx = None
        self.num_blocks = len(self.blocks)
        if self.input:
            for i in range(len(input['kwargs'])):
                if 'use_cache' in input['kwargs'][i]:
                    input['kwargs'][i].pop('use_cache')
            for i in range(len(input['kwargs'])):
                if 'past_key_value' in input['kwargs'][i]:
                    input['kwargs'][i]['past_key_value'] = None
            self.n_samples = 0
            for i in range(len(input['data'])):
                self.n_samples += input['data'][i].shape[0]

    def run_block_loop(self):
        for i in range(len(self.blocks)):
            self.block_idx = i
            logger.info(
                f'\nblock index: {self.block_idx}/{len(self.blocks)} '
                f'\nblock: {self.blocks[self.block_idx]}'
            )
            self.block_opt(self.blocks[self.block_idx])

        if hasattr(self, 'save_scale') and self.save_scale:
            torch.save(self.act_scales, self.scale_path)
        if hasattr(self, 'save_clip') and self.save_clip:
            torch.save(self.weight_clips, self.clip_path)

    @abstractmethod
    def block_opt(self, block):
        pass

    def cache_input_hook(self, m, x, y, name, feat_dict):
        x = x[0]
        x = x.detach().cpu()
        feat_dict[name].append(x)
