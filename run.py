import argparse
import sys
from pathlib import Path
import time
import torch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from utils.run_utils import load_config, merge_cli  # noqa: E402

from src.config import EngineConfig  # noqa: E402
from vllm_re.src.engine.engine import Engine  # noqa: E402
from src.model_hub import (  # noqa: E402
    DEFAULT_CACHE_DIR,
    QWEN3_REGISTRY,
    download_models,
    resolve_model_path,
)
from src.sampling_params import SamplingParams

DEFAULTS = {
    "model": "Qwen3-0.6B",
    "prompt": "Hello, my name is",
    "max_new_tokens": 200,
    "temperature": 0.9,
    "top_p": 0.95,
    "top_k": 20,
    "enforce_eager": True,
    "dtype": "auto",
    "context_len": 2048,
    "block_size": 16,
    "prefix_backend": "hash",
    "max_num_seqs": 8,
    "max_num_batched_tokens": 4096,
    "mix_prefill_decode": True,
}

def run_single(cfg:dict) -> None:
    model_path = resolve_model_path(cfg["model"])
    print(f"Loading {model_path} ...")
    engine = Engine(
        EngineConfig(
            model=str(model_path),
            # enforce_eager=cfg["enforce_eager"],
            dtype=cfg["dtype"],
            context_len=cfg["context_len"],
            block_size=cfg["block_size"],
            prefix_backend=cfg["prefix_backend"],
        ))

    prompt = cfg["prompt"]
    print(f"Prompt: {prompt}\n{prompt}", end="", flush=True)
    start = time.perf_counter()
    n = 0
    for tok in engine.generate(
        prompt,
        max_new_tokens=cfg["max_new_tokens"],
        temperature=cfg["temperature"],
        top_p=cfg["top_p"],
        top_k=cfg["top_k"],
    ):
        print(tok,end="",flush=True)
        n += 1
    elapsed = time.perf_counter() - start
    #重要指标单次请求的平均生成吞吐率tok/s
    print(f"\n\n{n} tokens in {elapsed:.2f}s ({n / elapsed:.1f} tok/s)")

def run_batch(cfg:dict) -> None:
    model_path = resolve_model_path(cfg["model"])
    print(f"Loading {model_path} for continuous batching ...")
    engine = Engine(
        EngineConfig(
            model=str(model_path),
            prefix_backend=cfg["prefix_backend"],
            max_num_seqs=cfg["max_num_seqs"],
            max_num_batched_tokens=cfg["max_num_batched_tokens"],
            mix_prefill_decode=cfg["mix_prefill_decode"],
        )
    )

    prompts = [
        "Hello, my name is",
        "Once upon a time",
        "The capital of France is",
        "In machine learning,",
    ]
    sp = SamplingParams(temperature=0.0, max_tokens=cfg["max_new_tokens"])
    t0 = time.perf_counter()
    outs = engine.generate_batch(prompts, sp)
    dt = time.perf_counter() - t0
    for i, (prompt, text) in enumerate(zip(prompts, outs)):
        print(f"\n[{i}] {prompt!r}\n    → {text!r}")
    total_tok = sum(len(engine.tokenizer.encode(o)) for o in outs)
    print(f"\n{len(prompts)} requests, ~{total_tok} toks in {dt:.2f}s")



def main() -> None:
    p = argparse.ArgumentParser(description="vllm-v2 Continuous Batching")
    p.add_argument("--config",default=None,help="YAML 配置文件路径")
    p.add_argument("--model",default=None)
    p.add_argument("--prompt",default=None)
    p.add_argument("--max-new-tokens",type=int,default=None)
    p.add_argument("--temperature",type=float,default=None)
    p.add_argument("--top-p",type=float,default=None)
    p.add_argument("--top-k", type=int, default=None)
    p.add_argument("--download", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--block-size", type=int, default=None)
    p.add_argument("--prefix-backend", choices=("none", "hash"), default=None)
    p.add_argument("--max-num-seqs", type=int, default=None)
    p.add_argument("--batch", action="store_true", help="运行批量推理演示")
    args = p.parse_args()

    if args.list:
        print(f"Cache: {DEFAULT_CACHE_DIR}")
        print(f"Models: {', '.join(s.local_name for s in QWEN3_REGISTRY)}")
        return
    if args.download:
        print(f"Downloading to {DEFAULT_CACHE_DIR} ...")
        download_models()
        print("Done.")
        return

    config_path = Path(args.config) if args.config else HERE / "config.yaml"
    cfg = load_config(config_path,DEFAULTS)
    merge_cli(
        cfg,
        args,
        ("model", "prompt", "max_new_tokens", "temperature", "top_p", "top_k", "block_size", "prefix_backend", "max_num_seqs"),
    )

    if args.batch:
        run_batch(cfg)
    else:
        run_single(cfg)

if __name__ == "__main__":
    main()
