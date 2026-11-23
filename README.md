# vllm_re
> 以 Qwen3 为基准，从零实现自回归推理，支持分页 KV 缓存和跨请求的哈希前缀复用。

## 性能对比

基线是 `v0` 标签（`6929e41`），每步重新计算全部历史 token。分页 KV 缓存版`v1` 标签（`71ce959`）在 prefill 后每步只对一个新 token 做前向，注意力层从页池读取历史 K/V。

测试使用 NVIDIA GeForce RTX 3050 Ti Laptop GPU（4 GiB）、同一份 Qwen3-0.6B 权重、BF16、eager 模式和贪心解码。预热后每项测 3 次，表中为中位数。计时从调用生成接口开始，排除模型加载和终端打印。冷请求在空前缀缓存下运行。

| 冷请求场景 | 指标 | v0 | KV 缓存版 | 变化 |
| --- | --- | ---: | ---: | ---: |
| 输入 5 token，输出 200 token | 生成吞吐 | 19.45 tok/s | 19.06 tok/s | 基本持平 |
| 输入 512 token，输出 32 token | 生成吞吐 | 8.15 tok/s | **20.88 tok/s** | **2.56 倍（+156%）** |
| 输入 512 token，输出 32 token | 首 token 后平均单 token 耗时 | 123.04 ms | **45.76 ms** | **下降 62.8%** |
| 输入 512 token，输出 32 token | 首 token 延迟（TTFT） | 109.34 ms | 115.18 ms | 增加 5.3% |
| 输入 5 token，输出 200 token | PyTorch 峰值显存分配量 | 1,464 MiB | 1,912 MiB | 增加约 448 MiB |

在同一个 KV 缓存版引擎中，两次请求同一段 513-token 输入并各生成 1 个 token。第二次请求复用前 512 个 token 的 KV，首 token 延迟从 **121.31 ms 降至 49.52 ms（下降 59.2%）**。这是 KV 缓存版内部的冷、热缓存对比。默认块大小为 16 token，5-token 的短 prompt 本身凑不满一个可复用块，每次重新运行 `python run.py` 也会创建新的页池。

## 项目结构

```
vllm_re/
├── run.py                        # CLI 入口
├── config.yaml                   # 默认推理参数
├── .gitignore                    # 忽略 __pycache__ / *.pyc 等
├── src/
│   ├── config.py                 # 模型配置
│   ├── model_hub.py              # 模型注册表
│   ├── engine.py                 # 自回归循环
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
Engine.generate()
  ├─ 匹配前缀缓存，为剩余的 prompt 分配 KV 页
  ├─ Prefill 未命中的 prompt，采样首个 token
  ├─ Decode 每步输入一个新 token，从页池读取历史 K/V
  └─ 请求结束后登记 KV 已写完的整块，释放其余页
```

如果整个 prompt 命中缓存，引擎会重算最后一个位置来获取首个输出 token 的 logits。

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

# 列出可用模型
python run.py --list
```
