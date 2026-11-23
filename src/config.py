from __future__ import annotations
import os
from dataclasses import dataclass,field
from transformers import AutoConfig,PretrainedConfig


@dataclass
class EngineConfig:
    model: str # 模型路径，定位加载对应模型
    context_len: int = 2048 # 最大上下文窗口
    enforce_eager: bool = True # 执行eager,不适用CUDA Graph
    dtype: str = "auto"
    hf_config: PretrainedConfig = field(init=False)
    prefix_backend: Literal["none", "hash"] = "hash"
    block_size: int = 16
    num_kv_blocks: int = 0  # 0 → 按 context_len 推导

    def __post_init__(self) -> None:
        # expanduser 路径中~替换为真实系统路径
        self.model = os.path.expanduser(self.model)
        # 加载模型 config.json 返回 PretrainedConfig 对象
        # trust_remote_code 允许加载模型自定义配置代码
        self.hf_config = AutoConfig.from_pretrained(self.model,trust_remote_code=True)
        if self.prefix_backend not in ("none", "hash", ):
            raise ValueError(
                f"prefix_backend must be none|hash, got {self.prefix_backend!r}"
            )
        if self.block_size <= 0:
            raise ValueError("block_size must be > 0")
        if self.num_kv_blocks <=0:
            need = (self.context_len + self.block_size - 1) // self.block_size
            self.num_kv_blocks = max(need * 2, need + 8)