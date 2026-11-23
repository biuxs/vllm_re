""" 满块链式哈希 """

from __future__ import annotations

import hashlib
import struct

def compute_block_hash(token_ids: list[int], prefix:int = -1) -> int:
    """ 本块 tokens（可选缀上一段块哈希）→ 64bit 无符号哈希。 """

    # blake2 是现代高速密码哈希算法 blake2b 内部规范小端，面向64位系统
    h  = hashlib.blake2b(digest_size=8)
    if prefix != -1: 
        # 把上一节的输入按照小端方式，无符号整数处理成二进制数
        h.update(prefix.to_bytes(8, "little", signed=False))
    # struct.pack python数据转换成二进制数 其中 < 代表小端序 i 代表int 四字节  
    h.update(struct.pack(f"<{len(token_ids)}i", *token_ids))
    
    return int.from_bytes(h.digest(), "little", signed=False)