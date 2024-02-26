# Copyright (c) 2023 Michael Hu.
# This project is released under the MIT License.
# See the accompanying LICENSE file for details.


# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the Llama 2 Community License Agreement.

import math
from dataclasses import dataclass, asdict
from typing import Any, Optional, Tuple, Iterable, Union, Dict
from functools import partial
from typing_extensions import Self
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.checkpoint import checkpoint

# llama 2 models
llama_configs = {
    '7B': dict(n_layers=32, n_heads=32, dim=4096),
    '13B': dict(n_layers=40, n_heads=40, dim=5120),
    '70B': dict(n_layers=80, n_heads=64, dim=8192),
}


supported_model_types = set(llama_configs.keys())


@dataclass
class ModelArgs:
    dim: int = 4096
    n_layers: int = 32
    n_heads: int = 32
    vocab_size: int = 32000
    multiple_of: int = 256  # make SwiGLU hidden layer size multiple of large power of 2
    ffn_dim_multiplier: Optional[float] = None
    norm_eps: float = 1e-5

    max_batch_size: int = 8  # for attention key, value caches
    max_seq_len: int = 2048

    use_cache: bool = False  # should only use cache when do inference

    # dropout regularization
    embed_dropout: float = 0.0
    attn_dropout: float = 0.0
    llm_align_dropout: float = 0.0

    # others
    gradient_checkpointing: bool = False

    def dict(self):
        return {k: str(v) if not isinstance(v, (float, int, bool, type(None))) else v for k, v in asdict(self).items()}

    @classmethod
    def from_model_type(cls, model_type: str, **kwargs) -> Self:
        assert model_type in supported_model_types

        config = llama_configs[model_type]
        config.update(kwargs)

        return cls(**config)


class RMSNorm(torch.nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6):
        super().__init__()
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(dim))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        output = self._norm(x.float())
        return (output * self.weight).type_as(x)


def precompute_freqs_cis(dim: int, end: int, theta: float = 10000.0):
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    t = torch.arange(end, device=freqs.device)  # type: ignore
    freqs = torch.outer(t, freqs).float()  # type: ignore
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)  # complex64
    return freqs_cis


def reshape_for_broadcast(freqs_cis: torch.Tensor, x: torch.Tensor):
    ndim = x.ndim
    assert 0 <= 1 < ndim
    assert freqs_cis.shape == (x.shape[1], x.shape[-1])
    shape = [d if i == 1 or i == ndim - 1 else 1 for i, d in enumerate(x.shape)]
    return freqs_cis.view(*shape)


def apply_rotary_emb(
    xq: torch.Tensor,
    xk: torch.Tensor,
    freqs_cis: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor]:
    xq_ = torch.view_as_complex(xq.float().reshape(*xq.shape[:-1], -1, 2))
    xk_ = torch.view_as_complex(xk.float().reshape(*xk.shape[:-1], -1, 2))
    freqs_cis = reshape_for_broadcast(freqs_cis, xq_)
    xq_out = torch.view_as_real(xq_ * freqs_cis).flatten(3)
    xk_out = torch.view_as_real(xk_ * freqs_cis).flatten(3)
    return xq_out.type_as(xq), xk_out.type_as(xk)


class Attention(nn.Module):
    def __init__(self, args: ModelArgs):
        super().__init__()
        self.max_batch_size = args.max_batch_size
        self.max_seq_len = args.max_seq_len
        self.n_heads = args.n_heads
        self.head_dim = args.dim // args.n_heads

        self.wq = nn.Linear(args.dim, args.n_heads * self.head_dim, bias=False)
        self.wk = nn.Linear(args.dim, self.n_heads * self.head_dim, bias=False)
        self.wv = nn.Linear(args.dim, self.n_heads * self.head_dim, bias=False)
        self.wo = nn.Linear(args.n_heads * self.head_dim, args.dim, bias=False)

        self.use_cache = args.use_cache

        self.cache_k = None
        self.cache_v = None
        if self.use_cache:
            self.cache_k = torch.zeros((self.max_batch_size, self.max_seq_len, self.n_heads, self.head_dim))
            self.cache_v = torch.zeros((self.max_batch_size, self.max_seq_len, self.n_heads, self.head_dim))

        # regularization
        self.attn_dropout = nn.Dropout(args.attn_dropout) if args.attn_dropout > 0 else nn.Identity()

    def forward(
        self,
        x: torch.Tensor,
        start_pos: int,
        freqs_cis: torch.Tensor,
        mask: Optional[torch.Tensor],
    ):
        bsz, seqlen, _ = x.shape
        xq, xk, xv = self.wq(x), self.wk(x), self.wv(x)

        xq = xq.view(bsz, seqlen, self.n_heads, self.head_dim)
        xk = xk.view(bsz, seqlen, self.n_heads, self.head_dim)
        xv = xv.view(bsz, seqlen, self.n_heads, self.head_dim)

        xq, xk = apply_rotary_emb(xq, xk, freqs_cis=freqs_cis)

        if self.use_cache:
            # should only use cache when do inference
            self.cache_k = self.cache_k.to(xq)
            self.cache_v = self.cache_v.to(xq)

            self.cache_k[:bsz, start_pos : start_pos + seqlen] = xk
            self.cache_v[:bsz, start_pos : start_pos + seqlen] = xv

            keys = self.cache_k[:bsz, : start_pos + seqlen]
            values = self.cache_v[:bsz, : start_pos + seqlen]
        else:
            keys = xk
            values = xv

        xq = xq.transpose(1, 2)  # (bs, n_heads, cache_len + seqlen, head_dim)
        keys = keys.transpose(1, 2)  # (bs, n_heads, cache_len + seqlen, head_dim)
        values = values.transpose(1, 2)  # (bs, n_heads, cache_len + seqlen, head_dim)

        scores = torch.matmul(xq, keys.transpose(2, 3)) / math.sqrt(self.head_dim)
        if mask is not None:
            scores = scores + mask  # (bs, n_heads, seqlen, cache_len + seqlen)
        scores = F.softmax(scores.float(), dim=-1).type_as(xq)
        scores = self.attn_dropout(scores)
        output = torch.matmul(scores, values)  # (bs, n_heads, seqlen, head_dim)
        output = output.transpose(1, 2).contiguous().view(bsz, seqlen, -1)
        output = self.wo(output)
        return output


class FeedForward(nn.Module):
    def __init__(
        self,
        dim: int,
        hidden_dim: int,
        multiple_of: int,
        ffn_dim_multiplier: Optional[float] = None,
    ):
        super().__init__()
        hidden_dim = int(2 * hidden_dim / 3)
        # custom dim factor multiplier
        if ffn_dim_multiplier is not None:
            hidden_dim = int(ffn_dim_multiplier * hidden_dim)
        hidden_dim = multiple_of * ((hidden_dim + multiple_of - 1) // multiple_of)

        self.w1 = nn.Linear(dim, hidden_dim, bias=False)
        self.w2 = nn.Linear(hidden_dim, dim, bias=False)
        self.w3 = nn.Linear(dim, hidden_dim, bias=False)

    def forward(self, x):
        output = self.w2(F.silu(self.w1(x)) * self.w3(x))
        return output


class TransformerBlock(nn.Module):
    def __init__(self, layer_id: int, args: ModelArgs):
        super().__init__()
        self.n_heads = args.n_heads
        self.dim = args.dim
        self.head_dim = args.dim // args.n_heads
        self.attention = Attention(args)
        self.feed_forward = FeedForward(
            dim=args.dim,
            hidden_dim=4 * args.dim,
            multiple_of=args.multiple_of,
            ffn_dim_multiplier=args.ffn_dim_multiplier,
        )
        self.layer_id = layer_id
        self.attention_norm = RMSNorm(args.dim, eps=args.norm_eps)
        self.ffn_norm = RMSNorm(args.dim, eps=args.norm_eps)

    def forward(
        self,
        x: torch.Tensor,
        start_pos: int,
        freqs_cis: torch.Tensor,
        mask: Optional[torch.Tensor],
    ):
        h = x + self.attention.forward(self.attention_norm(x), start_pos, freqs_cis, mask)
        out = h + self.feed_forward.forward(self.ffn_norm(h))
        return out


class Transformer(nn.Module):
    def __init__(self, params: ModelArgs):
        super().__init__()
        self.params = params
        self.vocab_size = params.vocab_size
        self.n_layers = params.n_layers

        self.token_embeddings = nn.Embedding(params.vocab_size, params.dim)
        self.embeddings_dropout = nn.Dropout(params.embed_dropout) if params.embed_dropout > 0 else nn.Identity()

        self.layers: Iterable[TransformerBlock] = torch.nn.ModuleList()
        for layer_id in range(params.n_layers):
            self.layers.append(TransformerBlock(layer_id, params))

        self.post_norm = RMSNorm(params.dim, eps=params.norm_eps)
        self.lm_head = nn.Linear(params.dim, params.vocab_size, bias=False)

        self.freqs_cis = precompute_freqs_cis(self.params.dim // self.params.n_heads, self.params.max_seq_len * 2)

        # Multi-modal LLM alignment projection layer, note the output embed size from ImageBind is always 1024
        self.llm_align_proj = nn.Linear(1024, params.dim, bias=False)
        self.llm_align_drop = nn.Dropout(params.llm_align_dropout) if params.llm_align_dropout > 0 else nn.Identity()

    def forward(
        self,
        tokens: torch.Tensor,
        start_pos: Optional[int] = 0,
        prompt_media_pos: Optional[torch.Tensor] = None,
        prompt_media_hidden: Optional[torch.Tensor] = None,  # [bs, num_medias, 1024]
    ) -> torch.Tensor:
        _bsz, seqlen = tokens.shape
        h = self.token_embeddings(tokens)
        h = self.embeddings_dropout(h)

        # handle multi-modal media input, where the features are pre-computed using ImageBind model
        if prompt_media_pos is not None and prompt_media_hidden is not None:
            assert len(prompt_media_pos.shape) == 2
            assert prompt_media_pos.shape[0] == prompt_media_hidden.shape[0]

            media_h = self.llm_align_proj(prompt_media_hidden.to(h.dtype))  # [bs, num_medias, params.dim]
            media_h = self.llm_align_drop(media_h)

            # replace the media placeholder token with media embed from the output of LLM projection layer
            _B, _T = prompt_media_pos.shape
            _H = h.shape[-1]
            h.scatter_(1, prompt_media_pos.unsqueeze(-1).expand(_B, _T, _H), media_h)

        self.freqs_cis = self.freqs_cis.to(h.device)
        freqs_cis = self.freqs_cis[start_pos : start_pos + seqlen]

        mask = None
        if seqlen > 1:
            mask = torch.full((seqlen, seqlen), float('-inf'), device=tokens.device)
            mask = torch.triu(mask, diagonal=1)

            # When performing key-value caching, we compute the attention scores
            # only for the new sequence. Thus, the matrix of scores is of size
            # (seqlen, cache_len + seqlen), and the only masked entries are (i, j) for
            # j > cache_len + i, since row i corresponds to token cache_len + i.
            mask = torch.hstack([torch.zeros((seqlen, start_pos), device=tokens.device), mask]).type_as(h)

        for layer in self.layers:
            if self.params.gradient_checkpointing and self.training:
                h = checkpoint(layer, h, start_pos, freqs_cis, mask, use_reentrant=False)
            else:
                h = layer(h, start_pos, freqs_cis, mask)

        h = self.post_norm(h)
        output = self.lm_head(h).float()
        return output


if __name__ == '__main__':
    for type in ('7B', '13B', '70B'):
        model_args = ModelArgs.from_model_type(type)

        print(model_args)
