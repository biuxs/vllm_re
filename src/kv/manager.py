""" KV 生命周期编排：allocate / ensure / finish + 前缀索引

典型流程:
    seq = manager.new_sequence(prompt_ids)
    manager.allocate(seq)          # 匹配前缀 + 分配余页
    ... Engine 绑 ForwardContext，Attention 用 store.write/gather ...
    manager.ensure_blocks(seq)     # decode 变长时补页
    manager.finish(seq)            # 发布满块进索引，页回 cached/free
"""

from __future__ import annotations

from typing import Literal

import torch
from torch import nn

from ..pagedattention.store import PagedStore
from .pool import BlockPool
from .prefix.hash_index import HashPrefixIndex
from .prefix.noop import NoopPrefixIndex
from .sequence import Sequence

_PrefixBackend = Literal["hash","none"]

def _make_index(backend: _PrefixBackend):
    """ 按配置创建前缀索引后端 """
    if backend == "hash":
        return HashPrefixIndex()
    return NoopPrefixIndex()

class KVManager:
    """ 单引擎的 KV 与前缀缓存入口 """

    def __init__(
        self,
        model: nn.Module, # 拿到 head_dim、num_kv_heads 等模型参数，分配 KV block 需要
        *,
        num_blocks: int, # 内存池总block数
        block_size: int, # 每个block存token数量
        prefix_backend: _PrefixBackend, # 控制索引
        device: torch.device,
        dtype: torch.dtype,
    ) -> None:
        self.block_size = block_size
        self.prefix_backend = prefix_backend
        self.pool = BlockPool.allocate(
            model,
            num_blocks=num_blocks,
            block_size=block_size,
            device=device,
            dtype=dtype,
        ) # 按照模型创建kv缓存池
        self.store = PagedStore(self.pool) # 读写 kv缓存池
        self.index = _make_index(prefix_backend)
        # 驱逐回调 内存池不够需要回收物理块bid
        self.pool.on_evict = lambda bid: self.index.invalidate(bid,self.pool)
        self._seq_counter = 0 # 请求数量
        self.last_num_cached_tokens = 0 # 上一轮迭代一共命中缓存的token数量，用于统计前缀缓存命中率

    def new_sequence(self,token_ids:list[int]) -> Sequence:
        """ 创建尚未绑页的 Sequence """
        self._seq_counter += 1
        return Sequence(
            token_ids=list(token_ids),
            block_size=self.block_size,
        )
    def _fit_prefix(self, seq: Sequence) -> int:
        """前缀复用预检查：计算当前序列最多可复用多少连续前缀 KV 块。"""
        need = seq.num_blocks
        if need ==0:
            return 0
        hit = self.index.match(seq,self.pool)
        max_cached = min(len(hit),need) #计划复用块
        #bolck三种状态：free、used、cached
        #前缀已经命中的块
        reclaimable = sum(1 for b in hit[:max_cached] if b in self.pool.cached_ids)
        other_cached = len(self.pool.cached_ids) - reclaimable
        new_needed = need - max_cached #复用之后需要的块
        if len(self.pool.free_ids) + other_cached >= new_needed:
            return max_cached
        return -1

    def can_allocate(self, seq: Sequence) -> bool:
        """ 判断新请求是否有足够物理块可分配 """
        return self._fit_prefix(seq) >= 0

    def allocate(self,seq:Sequence) -> None:
        """前缀命中复用页，其余 allocte_fresh;写好 block_table"""
        seq.block_table.clear()
        seq.num_cached_tokens = 0 #当前请求复用的前缀token数量
        need = seq.num_blocks
        if need == 0:
            self.last_num_cached_tokens = 0
            return
        max_cached = self._fit_prefix(seq)
        if max_cached < 0:
            raise RuntimeError("not enough KV blocks for sequence")
        hit = self.index.match(seq, self.pool)
        for bid in hit[:max_cached]:
            if bid in self.pool.used_ids:
                self.pool.blocks[bid].ref_count += 1 # 可复用块 引用加一
            elif bid in self.pool.cached_ids:
                self.pool.reclaim_cached(bid) # 引用，转为used块
            else:
                raise RuntimeError(f"prefix hit on non-resident block {bid}")
            seq.block_table.append(bid)

        for _ in range(max_cached,need):
            #先分配free状态的，需要复用的cached上面也转换成used了，free不够可以用其余的cached
            seq.block_table.append(self.pool.allocate_fresh())

        seq.num_cached_tokens = max_cached * self.block_size
        self.last_num_cached_tokens = seq.num_cached_tokens

    def can_append(self, seq: Sequence) -> bool:
            """ Decode阶段判断当前能不能给这个 sequence append 1 个新 token """
            """
            自回归解码流程：
            处理 token T，写入 T 的 KV
            ↓
            根据 logits 采样出 token T+1
            ↓
            把 T+1 append 到 seq.token_ids
            ↓
            下一轮以 T+1 为输入，写入 T+1 的 KV
            
            现在token T+1已经生成在seq中，但是T+1 还没有append 到 seq.token_ids
            """
            need_new = len(seq) % self.block_size == 1
            return (not need_new) or len(self.pool.free_ids) >= 1
    
    def may_append(self, seq: Sequence) -> None:
        """ Decode 生成阶段，执行真正的 block 分配动作 """
        if len(seq) % self.block_size == 1:
            seq.block_table.append(self.pool.allocate_fresh())

    def ensure_blocks(self,seq:Sequence) -> None:
        """ decode 变长时把 block_table 补到够用 """
        #由于num_blocks使用了@property 装饰器，所以每次读取属性时，都会自动根据num_tokens重新计算
        while len(seq.block_table) < seq.num_blocks:
            seq.block_table.append(self.pool.allocate_fresh())


    def sync_prefix(self,seq:Sequence) -> None:
            """ postprocess()调用 把这个序列已经完整填满的 KV block，发布到 prefix 索引 """
            n_full = seq.num_full_blocks()
            if n_full > 0 and self.prefix_backend !="none":
                self.index.publish(seq,n_full,self.pool)
    

    def deallocate(self, seq: Sequence,*,publish: bool) -> None:
        """ 释放一个 sequence 持有的全部物理 block """
        """ publish=False 任务被抢占暂停，不发布prefix缓存，尽量保留共享缓存数据 """
        """ publish=True 请求彻底跑完，把完整block提交给prefix缓存 """
        n_full = seq.num_full_blocks() if publish else 0
        if publish and n_full > 0:
            self.index.publish(seq,n_full,self.pool)
        
        published = set(seq.block_table[:n_full]) if n_full > 0 else set()
        for bid in list(seq.block_table):
            keep = bid in published and self.prefix_backend != "none"
            # hash 索引下，这个block不保留做共享缓存，从hash索引作废
            if not keep and self.prefix_backend == "hash":
                self.index.invalidate(bid, self.pool) # keep不是满块则不保留清理索引
            self.pool.release_to_cached_or_free(bid, keep_cached=keep) # 所有块更新物理池状态
        seq.block_table.clear()
        seq.num_cached_tokens = 0


    def finish(self, seq: Sequence) -> None:
        self.deallocate(seq, publish=True)