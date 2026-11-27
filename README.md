# vllm_re
> 以 Qwen3 为基准，从零实现自回归推理，支持分页 KV 缓存、哈希前缀复用、Continuous Batching 和 Chunked Prefill。

## 性能对比

### v0 → v1：分页 KV 缓存

基线是 `v0` 标签（`6929e41`），每步重新计算全部历史 token。分页 KV 缓存实现`v1` 标签（`9e33be5`）在 Prefill 后每步只对一个新 token 做前向，注意力层从页池读取历史 K/V。

测试使用 NVIDIA GeForce RTX 3050 Ti Laptop GPU（4 GiB）、同一份 Qwen3-0.6B 权重、BF16、eager 模式和贪心解码。预热后每项测 3 次，表中为中位数。计时从调用生成接口开始，排除模型加载和终端打印。冷请求在空前缀缓存下运行。

| 冷请求场景 | 指标 | v0 | KV 缓存版 | 变化 |
| --- | --- | ---: | ---: | ---: |
| 输入 5 token，输出 200 token | 生成吞吐 | 19.45 tok/s | 19.06 tok/s | 基本持平 |
| 输入 512 token，输出 32 token | 生成吞吐 | 8.15 tok/s | **20.88 tok/s** | **2.56 倍（+156%）** |
| 输入 512 token，输出 32 token | 首 token 后平均单 token 耗时 | 123.04 ms | **45.76 ms** | **下降 62.8%** |
| 输入 512 token，输出 32 token | 首 token 延迟（TTFT） | 109.34 ms | 115.18 ms | 增加 5.3% |
| 输入 5 token，输出 200 token | PyTorch 峰值显存分配量 | 1,464 MiB | 1,912 MiB | 增加约 448 MiB |

在同一个 KV 缓存版引擎中，两次请求同一段 513-token 输入并各生成 1 个 token。第二次请求复用前 512 个 token 的 KV，首 token 延迟从 **121.31 ms 降至 49.52 ms（下降 59.2%）**。这是 KV 缓存版内部的冷、热缓存对比。默认块大小为 16 token，5-token 的短 prompt 本身凑不满一个可复用块，每次重新运行 `python run.py` 也会创建新的页池。

### v1 → v2：Continuous Batching

`v1` 标签（`9e33be5`）一次只能执行一个请求；当前 v2 工作树基于 `v2` 标签，由 Scheduler 每轮动态组成 Decode batch，并在剩余 token budget 中调度 Prefill chunk。

测试使用同一块 RTX 3050 Ti Laptop GPU、Qwen3-0.6B、PyTorch 2.14.1+cu126、BF16、eager 模式和贪心解码。关闭 EOS 提前结束和前缀复用，固定 `block_size=16`、`context_len=512`、`num_kv_blocks=96`。预热后每项重复 3 次取中位数，排除模型加载时间。4 个请求视为同时到达；v1 按接口能力串行执行，v2 同时加入调度器。

| 场景 | v1 吞吐 | v2 吞吐 | 吞吐变化 | v1 P95 完成时间 | v2 P95 完成时间 | 完成时间变化 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 单请求：输入 5 token，输出 64 token | 19.60 tok/s | 19.58 tok/s | 基本持平 | 3.265 s | 3.268 s | 基本持平 |
| 4 请求：每条输入 5 token，输出 64 token | 13.69 tok/s | **39.25 tok/s** | **2.87 倍（+186.7%）** | 18.700 s | **6.523 s** | **下降 65.1%** |
| 4 请求：每条输入 256 token，输出 32 token | 15.46 tok/s | **38.41 tok/s** | **2.48 倍（+148.5%）** | 8.280 s | **3.333 s** | **下降 59.8%** |
| 4 请求：输入分别为 16/64/128/256 token，各输出 32 token | 19.25 tok/s | **35.90 tok/s** | **1.86 倍（+86.4%）** | 6.648 s | **3.566 s** | **下降 46.4%** |

共同到达时，v2 也显著降低了排在后面的请求等待首 token 的时间：

| 4 请求场景 | v1 P95 TTFT | v2 P95 TTFT | 变化 |
| --- | ---: | ---: | ---: |
| 每条输入 5 token，输出 64 token | 14.274 s | **0.227 s** | **下降 98.4%** |
| 每条输入 256 token，输出 32 token | 6.271 s | **0.381 s** | **下降 93.9%** |
| 混合输入 16/64/128/256 token | 5.128 s | **0.225 s** | **下降 95.6%** |

单请求吞吐没有改善，因为 v2 没有减少单条请求的模型计算；收益来自将多个请求的 Decode token 合并成真实张量 batch。固定 KV 页池下，4 请求测试的 PyTorch 峰值 allocated 显存仅增加约 0.4～2 MiB。

### Chunked Prefill 的延迟权衡

在线干扰测试先让一个请求进入 Decode，再加入 448-token 长请求。v2 使用 `max_num_batched_tokens=64` 和 Decode-first 混合调度：

| 指标 | v1 | v2 | 变化 |
| --- | ---: | ---: | ---: |
| 已有 Decode 请求的最大输出间隔 | 183.47 ms | **121.96 ms** | **下降 33.5%** |
| 新长请求 TTFT | **112.67 ms** | 866.64 ms | 增至 7.69 倍 |

Chunked Prefill 的目标是避免长 Prompt 一次占满整个调度步，从而降低在途 Decode 请求的卡顿；较小的 token budget 会让长请求分成更多 chunk，因此它自己的 TTFT 会升高。这是延迟隔离策略，不是所有请求延迟同时下降。

## 项目结构

```
vllm_re/
├── run.py                        # CLI 入口
├── config.yaml                   # 默认推理参数
├── .gitignore                    # 忽略 __pycache__ / *.pyc 等
├── src/
│   ├── config.py                 # 模型配置
│   ├── model_hub.py              # 模型注册表
│   ├── engine/
│   │   ├── engine.py             # 请求入口与 step 循环
│   │   ├── scheduler.py          # Continuous Batching / Chunked Prefill
│   │   └── model_runner.py       # Prefill 与批量 Decode 执行
│   ├── model.py                  # Qwen3 骨架
│   ├── model_loader.py           # safetensors 权重装载
│   ├── sampler.py                # 采样策略
│   ├── kv/                       # 物理页分配、回收与前缀索引
│   ├── pagedattention/           # KV 槽位映射与按页读写
│   └── layers/                   # 算子层
│       ├── attention.py          #   手写 GQA + 因果掩码
│       ├── activation.py         #   SiluAndMul
│       ├── layernorm.py          #   RMSNorm
│       └── rotary.py             #   RoPE 位置编码
└── utils/
    └── run_utils.py              # 配置加载与 CLI 参数合并
```

### 自回归执行流

```
Engine.add_request()
  └─ Sequence 进入 Scheduler.waiting

Engine.step()
  ├─ Scheduler.schedule()
  │   ├─ Decode-first 动态组 batch
  │   └─ 按 token budget 调度 Prefill chunk
  ├─ ModelRunner.run()
  │   ├─ Decode 请求组成真实张量 batch
  │   └─ Prefill 请求逐条执行
  ├─ Attention 写入新 K/V，并按 block table 读取历史 K/V
  └─ Scheduler.postprocess()
      ├─ 更新 num_computed_tokens
      ├─ 仅在最后一个 Prefill chunk 采样
      └─ 请求结束后只发布已完成计算的整块
```

`num_cached_tokens` 只记录本次分配命中的前缀长度，`num_computed_tokens` 才表示 KV 已真实写入的计算进度。即使整个 Prompt 命中缓存，引擎也会重算最后一个位置来获取首个输出 token 的 logits。

### 模型层

```
Qwen3ForCausalLM          # 表征与词表投影分离
└── Qwen3Model
    ├── embed_tokens      # nn.Embedding(vocab, hidden)
    ├── rotary_emd        # 各层共享的 RoPE
    ├── layers            # num_hidden_layers × Qwen3DecoderLayer
    │   └── Qwen3DecoderLayer
    │       ├── input_layernorm       # RMSNorm
    │       ├── self_attn
    │       │   ├── qkv_proj          # 融合 QKV
    │       │   ├── q_norm / k_norm   # QK-Norm（Qwen3 特有）
    │       │   ├── Attention         # 手写 GQA + 因果掩码
    │       │   └── o_proj
    │       ├── post_attention_layernorm
    │       └── mlp
    │           ├── gate_proj / up_proj / down_proj
    │           └── act_fn            # SiluAndMul（SwiGLU）
    └── norm             # 最终 RMSNorm
```

## 快速开始

```bash
# 下载模型（默认缓存到 ~/huggingface）
python run.py --download

# 推理（默认从 config.yaml 读取参数）
python run.py --model Qwen3-0.6B --prompt "Hello, my name is"

# Continuous Batching 批量推理演示
python run.py --batch

# 列出可用模型
python run.py --list
```
