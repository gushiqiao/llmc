import torch
import torch.nn as nn
from loguru import logger
import functools
import gc
from .base_blockwise_quantization import BaseBlockwiseQuantization
from llmc.utils.registry_factory import ALGO_REGISTRY
from .module_utils import _LLMC_LN_TYPES_, _TRANSFORMERS_LN_TYPES_
from .module_utils import _LLMC_LINEAR_TYPES_, _TRANSFORMERS_LINEAR_TYPES_
from .module_utils import FakeQuantLinear, OriginFloatLinear
from collections import defaultdict


@ALGO_REGISTRY
class OsPlus(BaseBlockwiseQuantization):
    def __init__(self, model, quant_config, input, config):
        torch.set_grad_enabled(False)
        super().__init__(model, quant_config, input, config)
        if (
            "special" in self.quant_config
            and "weight_clip" in self.quant_config["special"]
        ):
            self.weight_clip = self.quant_config["special"]["weight_clip"]
        else:
            self.weight_clip = False
        if (
            "special" in self.quant_config
            and "save_scale" in self.quant_config["special"]
        ):
            self.save_scale = self.quant_config["special"]["save_scale"]
        else:
            self.save_scale = False

    @torch.no_grad()
    def filter_subset(self, subset, idx, len):
        if self.weight_clip:
            if idx == len - 1:
                return False
            else:
                return True
        else:
            prev_op = subset["prev_op"]
            if isinstance(prev_op[0], tuple(_LLMC_LN_TYPES_ + _TRANSFORMERS_LN_TYPES_)):
                return True
            else:
                return False

    def block_transform(self, block, input_feat, idx, block_kwargs):
        logger.info(f"Start transform the {idx + 1}-th block")
        subsets = self.model.get_subsets_in_block(block)
        named_linears = self.model.get_block_linears(block)
        name_list = list(named_linears.keys())

        def register_hooks(feat_dict):
            handles = [
                block.get_submodule(name).register_forward_hook(
                    functools.partial(
                        self.cache_input_hook, name=name, feat_dict=feat_dict
                    )
                )
                for name in name_list
            ]
            self.block_forward(block)
            for h in handles:
                h.remove()

        for index, subset in enumerate(subsets):
            input_feat_subset = defaultdict(list)
            register_hooks(input_feat_subset)

            prev_op = subset["prev_op"]
            layers_dict = subset["layers"]
            input_name = subset["input"][0]
            inspect_module = subset["inspect"]
            inspect_has_kwargs = subset["has_kwargs"]
            subset_kwargs = block_kwargs if inspect_has_kwargs else {}

            if self.filter_subset(subset, index, len(subsets)):
                self.subset_transform(
                    layers_dict,
                    input_feat_subset,
                    prev_op,
                    input_name,
                    inspect_module,
                    idx,
                    subset_kwargs,
                )

            params_dict = {
                "a_qdq": self.a_qdq if not self.w_only else None,
                "w_qdq": self.w_qdq,
            }
            self.model.replace_module_subset(
                FakeQuantLinear, block, subset, idx, params_dict
            )

        if self.weight_clip:
            self.model.replace_module_block(OriginFloatLinear, block, idx, {})

            clip_input_feat = defaultdict(list)
            register_hooks(clip_input_feat)

            logger.info("auto_clip start")

            params_dict = {"a_qdq": self.a_qdq, "w_qdq": self.w_qdq}
            self.model.replace_module_block(FakeQuantLinear, block, idx, params_dict)
            self.auto_clip(
                block,
                idx,
                clip_input_feat,
                n_sample_token=self.config.calib.seq_len,
                eps=3e-1,
            )

            logger.info("auto_clip finished")
        else:
            logger.info("disable weight clip")

        torch.cuda.empty_cache()
        logger.info(f"End transform the {idx + 1}-th block")

    @torch.no_grad()
    def get_original_out(self, x, inspect_module, subset_kwargs):
        with torch.no_grad():
            org_out = inspect_module(x, **subset_kwargs)
            if isinstance(org_out, tuple):
                org_out = org_out[0]
        return org_out

    @torch.no_grad()
    def search_scale_shift_subset(
        self, layers, input_feats, inspect_module, subset_kwargs
    ):
        org_sd = {k: v.cpu() for k, v in inspect_module.state_dict().items()}
        org_out_dict = {}

        for i in range(len(input_feats)):
            input_feats[i] = input_feats[i].to(next(inspect_module.parameters()).device)
            x = input_feats[i]

            if self.model.has_bias():
                if x.dim() == 3:
                    cmx = torch.amax(x, dim=(0, 1))
                    cmn = torch.amin(x, dim=(0, 1))
                elif x.dim() == 2:
                    cmx = torch.amax(x, dim=0)
                    cmn = torch.amin(x, dim=0)
                shift = (cmx + cmn) / 2
            else:
                shift = None

            if isinstance(subset_kwargs, list):
                kwargs = subset_kwargs[i]
            else:
                kwargs = subset_kwargs

            if len(input_feats) == 1:
                org_out = self.get_original_out(x, inspect_module, kwargs)
            else:
                if i in org_out_dict:
                    org_out = org_out_dict[i]
                else:
                    org_out = self.get_original_out(x, inspect_module, kwargs)
                    org_out_dict[i] = org_out

            if self.model.has_bias():
                x_shift = x - shift
            else:
                x_shift = x.clone()

            if x.dim() == 3:
                cmx = torch.amax(x_shift, dim=(0, 1))
                cmn = torch.amin(x_shift, dim=(0, 1))
            elif x.dim() == 2:
                cmx = torch.amax(x_shift, dim=0)
                cmn = torch.amin(x_shift, dim=0)
            amx = max(
                x_shift.max(), torch.tensor(0.0, dtype=x_shift.dtype).to(x_shift.device)
            )
            amn = min(
                x_shift.min(), torch.tensor(0.0, dtype=x_shift.dtype).to(x_shift.device)
            )

            num = max(100, int(amx / 0.5))

            best_loss = None
            bounds = (1.0, max(-amn.item(), amx.item()))
            step = (bounds[1] - bounds[0]) / num

            best_min_range = -bounds[1]
            best_max_range = bounds[1]
            st = bounds[1]
            cnt = 0
            while st >= bounds[0]:
                min_range = torch.tensor(-st, dtype=x_shift.dtype).to(x_shift.device)
                max_range = torch.tensor(st, dtype=x_shift.dtype).to(x_shift.device)

                mx_scale = torch.where(
                    cmx > max_range,
                    cmx / max_range,
                    torch.tensor(1.0, dtype=x_shift.dtype).to(x_shift.device),
                )
                mn_scale = torch.where(
                    cmn < min_range,
                    cmn / min_range,
                    torch.tensor(1.0, dtype=x_shift.dtype).to(x_shift.device),
                )
                cur_scale = torch.max(mx_scale, mn_scale)

                for fc in layers:
                    if self.model.has_bias():
                        fc.bias.data += shift @ fc.weight.data.T

                    fc.weight.mul_(cur_scale.view(1, -1))

                    fc.weight.data = self.wquantizer.fake_quant_weight_dynamic(
                        fc.weight.data
                    )

                x_shift_tmp = x_shift / cur_scale.view(1, -1)
                q_x = self.aquantizer.fake_quant_act_dynamic(x_shift_tmp)

                out = inspect_module(q_x, **kwargs)
                if isinstance(out, tuple):
                    out = out[0]

                loss = (org_out - out).pow(2).sum(-1).mean()

                if best_loss is None or best_loss > loss:
                    best_loss = loss
                    best_min_range = -st
                    best_max_range = st
                cnt += 1
                st -= step
                inspect_module.load_state_dict(org_sd)

            best_min_range = torch.tensor(best_min_range, dtype=x_shift.dtype).to(
                x_shift.device
            )
            best_max_range = torch.tensor(best_max_range, dtype=x_shift.dtype).to(
                x_shift.device
            )

            mn_scale = torch.where(
                cmn < best_min_range,
                cmn / best_min_range,
                torch.tensor(1.0, dtype=x_shift.dtype).to(x_shift.device),
            )
            mx_scale = torch.where(
                cmx > best_max_range,
                cmx / best_max_range,
                torch.tensor(1.0, dtype=x_shift.dtype).to(x_shift.device),
            )

            best_scale = torch.max(mx_scale, mn_scale)

            del org_out_dict
            gc.collect()
            torch.cuda.empty_cache()
            return best_scale, shift

    @torch.no_grad()
    def subset_transform(
        self,
        layers_dict,
        input_feat,
        prev_op,
        input_name,
        inspect_module,
        idx,
        subset_kwargs,
    ):
        assert (
            len(prev_op) == 1
        ), "Only support single prev_op. If multi prev_ops, code need to be updated."

        layers = list(layers_dict.values())
        logger.info(layers_dict)
        if (
            isinstance(
                prev_op[0], tuple(_LLMC_LINEAR_TYPES_ + _TRANSFORMERS_LINEAR_TYPES_)
            )
            and prev_op[0].out_features != layers[0].in_features * 3
            and prev_op[0].out_features != layers[0].in_features
        ):
            logger.info("Cannot apply scale. Do not transform this subset.")
            return

        scale, shift = self.search_scale_shift_subset(
            layers, input_feat[input_name], inspect_module, subset_kwargs
        )
        self.apply_shift(shift, prev_op, layers)
        self.apply_scale(scale, prev_op, layers)

        if self.save_scale:
            for n in layers_dict:
                layer_name = f"{self.model.block_name_prefix}.{idx}.{n}"
                self.act_scales[layer_name] = scale
