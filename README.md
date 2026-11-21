# vllm_re
> 以 Qwen3 为基准，从零实现完整的自回归推理流程

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
run.py ──> Engine.generate()
              │
              ├─ 1. token → ids
              ├─ 2. for _ in max_new_tokens:           自回归循环
              │       ├─ input_ids = 全部历史 ids
              │       ├─ model(input_ids, positions)    完整前向
              │       ├─ compute_logits(h[:, -1:])      只取最后一步
              │       ├─ sampler(logits)                采样出新 token
              │       └─ ids.append(new_id)
              └─ 3. yield 每个新 token
```

### 模型层
```
Qwen3ForCausalLM          # 表征与词表投影分离
└── Qwen3Model
    ├── embed_tokens      # nn.Embedding(vocab, hidden)
    ├── layers            # num_hidden_layers × Qwen3DecoderLayer
    │   └── Qwen3DecoderLayer
    │       ├── input_layernorm       # RMSNorm
    │       ├── self_attn
    │       │   ├── qkv_proj          # 融合 QKV
    │       │   ├── q_norm / k_norm   # QK-Norm（Qwen3 特有）
    │       │   ├── rotary_emb        # RoPE
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
