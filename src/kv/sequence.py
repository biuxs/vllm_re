from __future__ import annotations

from dataclasses import dataclass,field

@dataclass
class Sequence:
    token_ids: list[int] # 保存这条请求全部逻辑token，包括输入prompt加decode已经生成出来的token
    block_size: int # 每个物理block可以存放的token数

    block_table: list[int] = field(default_factory=list) # 逻辑块索引到物理块索引的映射 PagedAttention页表
    num_cached_tokens: int = 0
    num_computed_tokens: int = 0  # 已有有效 KV 的连续前缀长度（包括缓存命中）


    @property
    def num_tokens(self) -> int:
        """ 当前序列长度 """
        return len(self.token_ids)


    @property
    def num_blocks(self) -> int:
        """ 容纳当前 token 所需页数（向上取整） """
        n = self.num_tokens
        if n == 0:
            return 0 
        return (n+self.block_size -1) // self.block_size

    def num_full_blocks(self) -> int:
        """ 当前请求可以填满的页数（尾块未满不计入，前缀只共享满块） """
        return self.num_tokens // self.block_size

    def block_token_ids(self,i:int) -> list[int]:
        "取出第i个逻辑页的token_id片段（用于算块哈希）"
        s = i*self.block_size
        return self.token_ids[s:s+self.block_size]

    def append_token(self, token_id: int) -> None:
        """ decode 追加一个新 token """
        self.token_ids.append(token_id)
