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

    def __post_init__(self) -> None:
        self.model = os.path.expanduser(self.model)
        # 加载模型 config.json 返回 PretrainedConfig 对象
        # trust_remote_code 允许加载模型自定义配置代码
        self.hf_config = AutoConfig.from_pretrained(self.model,trust_remote_code=True)
