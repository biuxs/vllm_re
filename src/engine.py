from __future__ import annotations

from collections.abc import Iterator

import torch
from transformers import AutoTokenizer

from .kv.context import ForwardContext, set_forward_context
from .kv.manager import KVManager
from .kv.sequence import Sequence
from .config import EngineConfig
from .model import Qwen3ForCausalLM
from .model_loader import init_context, load_model
from .sampler import Sampler
from .pagedattention import slots_tensor

class Engine:
    def __init__(self,config:EngineConfig) -> None:
        self.config = config
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.dtype = self._resolve_dtype()
        # 1 加载模型分词器 即 tokenizer.json tokenizer_config_json
        # 2 分词器负责将prompt转成token_id给模型，模型输出token_id转回文本
        # 3 trust_remote_code 允许加载模型自定义配置代码
        self.tokenizer = AutoTokenizer.from_pretrained(
            config.model,trust_remote_code=True
        )

        # 1 pad_token 填充token 做batch批量处理的时候，不同长度输入要补齐到相同长度，就用pad_token补充
        # 2 很多大模型原生没有自定义pad_token 就用eos_token代替
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.eos_token_id = self.tokenizer.eos_token_id

        self.context_len = self.config.context_len


        with init_context(self.dtype,self.device):
            self.model = Qwen3ForCausalLM(config.hf_config)

        load_model(self.model,config.model)
        self.model = self.model.eval()

        if not config.enforce_eager and self.device.type == "cuda":
            self.model = torch.compile(self.model,mode="reduce-overhead")

        self.sampler = Sampler()
        self._init_kv()
        self.last_num_cached_tokens=0

    def _init_kv(self) -> None:
        self.kv = KVManager(
            self.model,
            num_blocks=self.config.num_kv_blocks,
            block_size=self.config.block_size,
            prefix_backend=self.config.prefix_backend,
            device=self.device,
            dtype=self.dtype,
        )

    def reset_kv(
            self,prefix_backend: Literal["none","hash"] | None = None,
    ) -> None:
        """清空 KV / 前缀状态；可选切换 prefix_backend。供测试复用同一模型 """
        if prefix_backend is not None:
            if prefix_backend not in ("none","hash"):
                raise ValueError(f"invalid prefix_backend: {prefix_backend!r}")
            self.config.prefix_backend = prefix_backend
        old = getattr(self,"kv",None)
        self._init_kv()
        self.last_num_cached_tokens = 0
        if old is not None:
            del old
            if self.device.type == "cuda":
                torch.cuda.empty_cache()

#在模型执行一次 Forward 之前，把当前请求的 KV Cache 操作所需的信息封装成 ForwardContext，提供给 Attention 层使用。
    def _bind_ctx(
        self,
        seq: Sequence,
        *,
        is_prefill: bool,
        cache_len: int,
        write_end: int,
    ) -> None:
        #计算本次 KV 应该写到哪里
        slots = slots_tensor(
            seq.block_table,
            cache_len,
            write_end,
            self.kv.block_size,
            device=self.device,
        )
        #安装 ForwardContext,供后续模型内部的 Attention 层读取。
        set_forward_context(
            ForwardContext(
                is_prefill=is_prefill,
                cache_len=cache_len,
                seq_len=write_end,
                slot_mapping=slots,
                block_table=seq.block_table,
                store=self.kv.store,
            )
        )


    def _resolve_dtype(self) -> torch.dtype:
        if self.config.dtype != "auto":
            return getattr(torch,self.config.dtype)
        cfg = self.config.hf_config
        dt = getattr(self.config.hf_config,"dtype",None)
        return dt if isinstance(dt, torch.dtype) else torch.bfloat16

    @torch.inference_mode()
    def generate(
        self,
        prompt: str | list[int],
        max_new_tokens:int = 128,
        temperature: float = 0.0,
        top_p: float | None = None,
        top_k:int | None =None,
    ) :
        ids = (list(prompt) if isinstance(prompt,list) else self.tokenizer.encode(prompt))

        if not ids:
            raise ValueError("prompt must contain at least one token")
        if max_new_tokens < 0:
            raise ValueError("max_new_tokens must be non-negative")
        ctx = self.config.context_len #最长上下文长度
        if len(ids) > ctx:
            ids = ids[-ctx:]

        def pack(v,dtype):
            return None if v is None else torch.tensor([v],device=self.device,dtype=dtype)
        if temperature <= 0:
            # 贪心解码：采样参数全部关闭
            temp_t = top_p_t = top_k_t = None
        else:
            temp_t = pack(temperature,torch.float)
            top_p_t = pack(top_p,torch.float)
            top_k_t = pack(top_k,torch.long)

        seq:Sequence = self.kv.new_sequence(ids)
        self.kv.allocate(seq)# 分配block资源
        self.last_num_cached_tokens = seq.num_cached_tokens
        cached = seq.num_cached_tokens
        prompt_len = len(ids)

        try: #prefill逻辑
            if cached < prompt_len:
                self._bind_ctx(
                    seq,is_prefill=True,cache_len=cached,write_end=prompt_len,
                )
                new_ids = ids[cached:]

                input_ids = torch.tensor([new_ids], device=self.device,dtype=torch.long)
                positions =torch.arange(cached,prompt_len,device=self.device).unsqueeze(0)
                # 向前传播，输出最后一层隐藏状态h,shape=[1,seq_len,hidden_size]
                h = self.model(input_ids,positions)

                logits = self.model.compute_logits(h[:, -1:]).squeeze(1)

            else:# 整个prompt完全命中前缀缓存
                self._bind_ctx(
                    seq,is_prefill=False,cache_len=prompt_len - 1, write_end=prompt_len
                )#为了获得第一个输出 Token 的 logits，我们重新计算最后一个 Token
                input_ids = torch.tensor([[ids[-1]]], device=self.device, dtype=torch.long)
                positions = torch.tensor(
                    [[prompt_len - 1]], device=self.device, dtype=torch.long
                )
                # 需要最后一个token的输出hidden_state来采样第一个输出token
                h = self.model(input_ids, positions)
                logits = self.model.compute_logits(h[:, -1:]).squeeze(1)

            # prefill结束
            # item() 把0维标量张量数字取出，转换成python数字,之后转换成整数得到下一个token_id
            next_id = int(self.sampler(logits,temp_t,top_p_t,top_k_t).item())
            # 1 解码，首先将int输入包装成列表，之后解码翻译成对应的字符串片段
            yield self.tokenizer.decode([next_id])
            if next_id == self.eos_token_id or prompt_len >= self.context_len:
                    return
            seq.append_token(next_id)
            self.kv.ensure_blocks(seq)
            cached_len = prompt_len



            #开启decode循环
            for _ in range(max_new_tokens - 1):# 已经产出了一个token，需要循环生成最多max_new_token-1个
                if cached_len >= self.context_len:
                        break # 上下文上限保护
                self._bind_ctx(
                        seq,is_prefill=False,cache_len=cached_len,write_end=cached_len+1,
                    )
                ids = torch.tensor([[next_id]], device=self.device, dtype=torch.long)
                positions = torch.tensor([[cached_len]],device=self.device, dtype=torch.long)
                h = self.model(ids,positions)

                logits = self.model.compute_logits(h[:, -1:]).squeeze(1)
                #如果是贪心的话就是直接取最大的
                next_id = int(self.sampler(logits,temp_t,top_p_t, top_k_t).item())
                cached_len+=1


                seq.append_token(next_id)
                self.kv.ensure_blocks(seq)

                yield self.tokenizer.decode([next_id])
                if next_id == self.eos_token_id:
                    break
        finally:
            set_forward_context(None)
            self.kv.finish(seq)