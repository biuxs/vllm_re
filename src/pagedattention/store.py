"""按页读写物理 KV 页池"""
from __future__ import annotations
from typing import Protocol
import torch

class _BlockTensorStore(Protocol):
    # Protocol 协议类型，相当于接口定义,描述 KV 显存池对象必须具备的成员与张量 shape
    block_size: int
    num_kv_heads: int
    head_dim: int
    k: torch.Tensor  # [L, N_blocks, B, Hkv, D]
    v: torch.Tensor

def _coerce_kv_token_major(
        k:torch.Tensor,v:torch.Tensor
) -> tuple[torch.Tensor, torch.Tensor]:
    """ 把 KV 统一转换成 token 主维度格式 [total_tokens, Hkv, D] """
    if k.dim() == 4:
        b, s, h, d = k.shape
        return k.reshape(b * s, h, d), v.reshape(b * s, h, d)
    if k.dim() == 3:
        return k, v
    raise ValueError(f"unexpected k rank {k.dim()}")

class PagedStore:
    """ 分页 KV 存储：write（本步 K/V 写入 slot）+ gather（按页表读回历史）。

    物理布局 [num_layers, num_blocks, block_size, num_kv_heads, head_dim]。
    """
    def __init__(self,pool: _BlockTensorStore) -> None:
        """ 绑定一块物理页池。 """
        self.pool = pool

    @property
    def block_size(self) -> int:
        return self.pool.block_size

    @property
    def device(self) -> torch.device:
        return self.pool.k.device
    
    def write(
            self,
            layer_id:int,
            slot_mapping: torch.Tensor,
            k: torch.Tensor, # 本步算出的新的k和v，注意区分self.pool.k：跨推理步骤长期保存的完整 KV Cache，是写入目标。
            v: torch.Tensor,
    ) -> None:
        k, v = _coerce_kv_token_major(k, v)
        n = k.shape[0]
        if slot_mapping.numel() !=n:
            raise ValueError("Slot number mismatch between token number")

        flat_dim = self.pool.num_kv_heads * self.pool.head_dim
        flat_k = self.pool.k[layer_id].reshape(-1,flat_dim)
        flat_v = self.pool.v[layer_id].reshape(-1,flat_dim)

        with torch.no_grad():
            # 根据 slot_mapping 指定的物理槽位，把当前计算得到的 K 写入 KV Cache 的对应位置
            flat_k.index_copy_(0,slot_mapping,k.reshape(n,flat_dim))
            flat_v.index_copy_(0,slot_mapping,v.reshape(n,flat_dim))

    def gather(
        self,
        layer_id: int,
        block_table: list[int],
        seq_len: int, # 读取KV的token数
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """ 按逻辑顺序读回前缀 [0, seq_len) 的 K/V """
        # 函数调用举例：
        # 读请求1 gather(layer_id, block_table=[5, 9], seq_len=21)
        # 读请求2 gather(layer_id, block_table=[3, 7, 11], seq_len=35)
        if seq_len <= 0: # 没有token需要读
            empty = self.pool.k.new_empty(
                0,self.pool.num_kv_heads,self.pool.head_dim
            ) # 基于self.pool.k 得到空张量 此时shape=[0,num_kv_heads,head_dim]
            return empty, empty.clone() # kv都返回空张量

        bs = self.pool.block_size
        need_block = (seq_len + bs -1) // bs # 需要读取的物理块数
        if len(block_table) < need_block:
            raise IndexError("not enough blocks in block_table")

        device = self.pool.k.device
        table = torch.tensor(block_table[:need_block],device=device,dtype=torch.long)
        positions = torch.arange(seq_len,device=device,dtype=torch.long)
        physical = table[positions // bs] # 每个 token 的物理块号
        offsets = positions % bs # 每个 token 的页内偏移
        k = self.pool.k[layer_id,physical,offsets] # 返回的 k.shape=[seq_len, num_kv_heads, head_dim]
        v = self.pool.v[layer_id,physical,offsets]
        return k,v