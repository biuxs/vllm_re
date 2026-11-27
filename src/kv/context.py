""" 单次 forward 的页表合同（Engine → Attention） """

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import torch

if TYPE_CHECKING: # 静态检查时为True 实际允许时为False
    from ..pagedattention import PagedStore


@dataclass
class ForwardContext:
    """ Attention 只用页表合同 """

    is_prefill: bool
    store: PagedStore
    block_tables: list[list[int]]
    slot_mapping: torch.Tensor
    query_lens: list[int]
    context_lens: list[int]
    cached_lens: list[int] = field(default_factory=list)

_forward_ctx: ForwardContext | None = None

def set_forward_context(ctx: ForwardContext | None) -> None:
    """ 安装或清空当前线程的 ForwardContext """
    global _forward_ctx
    _forward_ctx = ctx

def get_forward_context() -> ForwardContext | None:
    """ 读取当前 ForwardContext；无则 Attention 走无缓存 SDPA """
    return _forward_ctx
