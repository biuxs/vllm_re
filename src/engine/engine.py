from __future__ import annotations

from collections.abc import Iterator

import torch
from transformers import AutoTokenizer

from ..kv.context import ForwardContext, set_forward_context
from ..kv.manager import KVManager
from ..kv.sequence import Sequence
from ..config import EngineConfig
from ..model import Qwen3ForCausalLM
from ..model_loader import init_context, load_model
from ..sampler import Sampler
from ..pagedattention import slots_tensor
from ..sampling_params import SamplingParams
from .model_runner import ModelRunner
from .scheduler import Scheduler

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
                # 请求调度器，请求排队，资源判断，状态更新
        self.scheduler = Scheduler(
            self.kv,
            max_num_seqs=self.config.max_num_seqs, # 最大请求数
            max_num_batched_tokens=self.config.max_num_batched_tokens, # 单轮token上限
            eos_token_id=self.eos_token_id, # 结束符，结合is_finished
            mix_prefill_decode=self.config.mix_prefill_decode, # 调度策略
        )
        # 模型执行器 接收Scheduler输出的序列列表向前传播并返回新的token_id
        self.model_runner = ModelRunner(
            self.model, self.kv, self.sampler, device=self.device
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
                store=self.kv.store,
                block_tables=[list(seq.block_table)],
                slot_mapping=slots,
                query_lens=[write_end - cache_len],
                context_lens=[write_end],
                cached_lens=[cache_len] if is_prefill else [],
            )
        )


    def _resolve_dtype(self) -> torch.dtype:
        if self.config.dtype != "auto":
            return getattr(torch,self.config.dtype)
        cfg = self.config.hf_config
        dt = getattr(self.config.hf_config,"dtype",None)
        return dt if isinstance(dt, torch.dtype) else torch.bfloat16

    def add_request(
        self,
        prompt: str | list[int],
        sampling_params: SamplingParams | None = None,
    ) -> Sequence:
        """ 将 prompt 包装成 Sequence 对象，并进入调度器排队 """
        token_ids = (
            list(prompt) if isinstance(prompt, list) else self.tokenizer.encode(prompt)
        )
        if len(token_ids) > self.context_len:
            token_ids = token_ids[-self.context_len :]
        seq = Sequence.from_prompt(
            token_ids, block_size=self.kv.block_size, sampling=sampling_params
        )
        self.scheduler.add(seq)
        return seq
    
    def step(self) -> tuple[list[tuple[int, list[int]]], int]:
        """负责从调度器获取本轮任务，交给模型执行，
        拿到输出 Token，
        再更新请求状态和 KV Cache，最后返回已经完成的请求及本轮计算量 """
        prefills, decodes = self.scheduler.schedule()

        if not prefills and not decodes:
            return [], 0

        # 来自 allocate() 的prefill阶段token命中的数量
        self.last_num_cached_tokens = self.kv.last_num_cached_tokens
        #本轮所有prefill跑的token数量
        prefill_tokens = sum(s.num_scheduled_tokens for s in prefills)
        #本轮所有的token数量
        metric = prefill_tokens + len(decodes) 

        #优先执行decode，保证正在流式输出的请求持续向前推进；之后再执行prefill分片
        p_ids, d_ids = self.model_runner.run(prefills,decodes)

        finished: list = []
        if decodes:
            finished.extend(
                self.scheduler.postprocess(decodes,d_ids,is_prefill=False)
            )
        # 正常情况下，prefill的seq是不会结束的，因为还要继续decode生成
        # 但是如果prompt过长超过限制token长度或者遇到eos,会提前结束
        if prefills:
            finished.extend(
                self.scheduler.postprocess(prefills, p_ids, is_prefill=True)
            )
        outputs = [
            (seq.seq_id, seq.token_ids[seq.num_prompt_tokens :]) for seq in finished
        ] # 返回单请求的请求id，decode输出token
        return outputs, metric
    

    def generate_batch(
        self,
        prompts: list[str | list[int]],
        sampling_params: SamplingParams | list[SamplingParams] | None = None,
    ) -> list[str]:
        """ 多请求对外API """
        # 处理采样参数，每条prompt对应一套采样配置
        if sampling_params is None:
            # 采用默认参数
            sps = [SamplingParams() for _ in prompts]
        elif isinstance(sampling_params, (list, tuple)):
            # 多prompt，采样配置数量必须和prompt数量对齐
            sps = list(sampling_params)
            if len(sps) != len(prompts):
                raise ValueError("sampling_params length must match prompts")
        else:
            # 单prompt
            sps = [sampling_params for _ in prompts]
        seqs = [self.add_request(p, sp) for p, sp in zip(prompts, sps)]
        finished_text: dict[int, str] = {}
        while not self.scheduler.is_finished():
            outputs, _ = self.step()
            for seq_id,completion_ids in outputs:
                finished_text[seq_id] = self.tokenizer.decode(completion_ids)
        return [finished_text[seq.seq_id] for seq in seqs]       

    def generate(
        self,
        prompt: str | list[int],
        max_new_tokens:int = 128,
        temperature: float = 0.0,
        top_p: float | None = None,
        top_k:int | None =None,
        min_p:float | None = None,
    ) -> Iterator[str]:
        """ 单请求对外API """
        sp = SamplingParams(
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            min_p=min_p,
            max_tokens=max_new_tokens,
        )
        seq = self.add_request(prompt, sp)
        emitted = 0 # 输出 token 计数器
        while not seq.is_finished:
            self.step() # 模型调度 + GPU 推理 + postprocess
            while emitted < seq.num_completion_tokens:
                #token 生成了，但还没输出
                tid = seq.token_ids[seq.num_prompt_tokens + emitted]
                emitted += 1
                yield self.tokenizer.decode([tid]) # 输出
    

        
