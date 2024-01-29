# Copyright (c) 2023 Michael Hu.
# This project is released under the MIT License.
# See the accompanying LICENSE file for details.


from typing import Tuple
from dataclasses import dataclass


@dataclass
class config:
    """Supervised fine-tuning using LoRA"""

    # model type definition, the details (number of layers, heads etc.) are defined in model.py
    model_type: str = '7B'  # 7B, 13B, 70B
    max_seq_len: int = 512

    pretrain_ckpt_file: str = './checkpoints/7b-pretrain/7B-steps-4400-consolidated.pth'  # load pretrained checkpoint
    tokenizer_file: str = '/home/michael/models/meta_llama2/tokenizer.model'  # load tokenizer model

    # datasets
    train_datasources: Tuple[str] = (
        './datasets/LLaVA_COCO/train.pkl',
        './datasets/VideoChat_WebVid/train.pkl',
    )
    val_datasources: Tuple[str] = (
        './datasets/LLaVA_COCO/validation.pkl',
        './datasets/VideoChat_WebVid/validation.pkl',
    )
    dataloader_workers: int = 1

    # if true, always pad the sequence to max_seq_len instead of current maximum length in the batch
    # this is helpful when starting out and try to found the hyperparameter (e.g batch size, maximum sequence length)
    # so we may sooner found out CUDA out of memory error, rather than hours into the training process
    full_pad: bool = False

    # training and validation loops
    num_epochs: int = 3
    train_batch_size: int = 2
    # accumulate gradients, where for step, the batch size is = train_batch_size x gradient_accum_steps
    gradient_accum_steps: int = 16
    loss_scale: float = 1.0 / 8  # scale loss to account for gradient accumulation, we don't want to use a very small scale
    val_interval: int = 400
    val_batch_size: int = 30
    val_steps: int = 40
    log_interval: int = 5  # log training metrics (loss, accuracy)
    ckpt_interval: int = 400  # save model checkpoints every N training steps

    # LoRA configuration
    lora_r: int = 64
    lora_scaling: float = 1.0  # set the LoRA scaling, by default 1.0 no scaling
    lora_dropout: float = 0.0

    # LoRA trainable layers
    lora_attn_query: bool = True  # train Attention query layer
    lora_attn_key: bool = False  # train Attention key layer
    lora_attn_value: bool = True  # train Attention value layer
    lora_attn_proj: bool = False  # train Attention projection layer
    lora_attn_mlp: bool = False  # train Attention MLP block
    lora_lm_head: bool = False  # train model output head

    train_bias: str = 'none'  # none, lora_only, all

    # Quantization
    quant_4bit: bool = False  # quantize frozen linear layer
    quant_lora_4bit: bool = False  # quantize LoRA linear layer
    quant_4bit_double: bool = False  # double quantize
    quant_4bit_type: str = 'nf4'  # only supports 'fp4' or 'nf4'

    # learning rate
    init_lr: float = 5e-5  # initial learning rate
    max_lr: float = 5e-4  # max learning rate after warm up
    min_lr: float = 1e-4  # min learning rate after decay
    warmup_ratio: float = 0.015

    # prompt is less important than completion
    prompt_loss_weight: float = 0.05  # we have multiple-turns for a single sample, which mostly are packed into the prompt tokens
    completion_loss_weight: float = 1.0

    # AdamW optimizer
    use_paged_adamw: bool = False
    weight_decay: float = 0.0
    adam_betas: Tuple = (0.9, 0.95)
    adam_eps: float = 1e-8
    adam_fused: bool = True  # only applicable if not using bitsandbytes optimizer
    grad_clip: float = 0.0

    # dropout regularization
    embed_dropout: float = 0.0
    attn_dropout: float = 0.0

    gradient_checkpointing: bool = False
    mixed_precision: bool = True  # default to BF16, but if no native GPU support detected, will use FP16.
    compile_model: bool = False  # not working with QLoRA

    # others
    seed: int = 127
    log_dir: str = './logs/finetune_lora'  # save logs and traces
    ckpt_dir: str = './checkpoints/finetune_lora'
    use_tensorboard: bool = True
    use_profiler: bool = False  # use torch profiler to monitoring traces, be careful as the logs will grow very fast
