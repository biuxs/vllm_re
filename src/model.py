import torch
from torch import nn
from transformers.models.qwen3 import Qwen3Config
from collections.abc import Iterable
from .layers.attention import Attention
from .layers.layernorm import RMSNorm
from .layers.activation import SiluAndMul
from .layers.rotary import RotaryEmbedding
from .model_loader import default_weight_loader

_QKV_SHARDS = (
    ("q_proj",lambda q,kv:(0,q)),
    ("k_proj",lambda q,kv:(q,kv)),
    ("v_proj",lambda q,kv:(q+kv,kv)),
)

#权重共享用于验证 Qwen3 推理流程，实际应用时可以注释掉
# class _TiedLinear(nn.Module):
#     def __init__(self,weight:torch.Tensor):
#         super().__init__()
#         self.weight = weight

#     def forward(self,x:torch.Tensor) ->torch.Tensor:
#         return torch.nn.functional.linear(x,self.weight)


class Qwen3MLP(nn.Module):
    def __init__(self,config:Qwen3Config):
        super().__init__()
        self.gate_proj = nn.Linear(config.hidden_size,config.intermediate_size,bias = False)
        self.up_proj = nn.Linear(config.hidden_size,config.intermediate_size,bias = False)
        self.down_proj = nn.Linear(config.intermediate_size,config.hidden_size,bias = False)
        self.act_fn = SiluAndMul()
    def forward(self,x:torch.Tensor):
        return self.down_proj(self.act_fn(self.gate_proj(x),self.up_proj(x)))


class Qwen3Attention(nn.Module):
    def __init__(self,config:Qwen3Config,layer_id:int):
        super().__init__()

        self.num_heads = config.num_attention_heads
        self.num_kv_heads = config.num_key_value_heads
        self.head_dim = config.head_dim
        self.q_size = self.num_heads * self.head_dim
        self.kv_size =self.num_kv_heads * self.head_dim

        #qkv_proj只会改变hidden_states的最后一个维度，所以q的最后一个维度是q_size所以后面需要unflatten成S，D
        self.qkv_proj = nn.Linear(config.hidden_size,self.q_size+2*self.kv_size,bias=False)

        self.o_proj = nn.Linear(self.q_size,config.hidden_size,bias=False)

        self.attn = Attention(self.num_heads,self.head_dim,self.num_kv_heads,layer_id)

        self.q_norm = RMSNorm(self.head_dim,eps = config.rms_norm_eps)
        self.k_norm = RMSNorm(self.head_dim,eps = config.rms_norm_eps)

    def forward(self,
                positions:torch.Tensor,
                hidden_states:torch.Tensor,
                rotary_emb:RotaryEmbedding
                ) -> torch.Tensor:
        q, k, v = self.qkv_proj(hidden_states).split(
            [self.q_size,self.kv_size,self.kv_size], dim=-1
        )

        q = self.q_norm(q.unflatten(-1,(-1,self.head_dim)))
        k = self.k_norm(k.unflatten(-1,(-1,self.head_dim)))

        v = v.unflatten(-1,(-1,self.head_dim))

        q,k = rotary_emb(positions,q,k)

        return self.o_proj(self.attn(q,k,v).flatten(-2))

class Qwen3DecoderLayer(nn.Module):
    def __init__(self,config:Qwen3Config,layer_id:int):
        super().__init__()
        self.self_attn = Qwen3Attention(config,layer_id)
        self.mlp = Qwen3MLP(config)
        self.input_layernorm = RMSNorm(config.hidden_size,eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size,eps=config.rms_norm_eps)

    def forward(self,
                positions:torch.Tensor,
                hidden_states:torch.Tensor,
                rotary_emd:RotaryEmbedding,
                residual:torch.Tensor | None
                ) -> tuple[torch.Tensor,torch.Tensor | None]:
        if residual is None:
            residual = hidden_states
            hidden_states = self.input_layernorm(hidden_states)
        else:
            hidden_states,residual = self.input_layernorm(hidden_states,residual)

        hidden_states = self.self_attn(positions,hidden_states,rotary_emd)

        hidden_states,residual = self.post_attention_layernorm(hidden_states,residual)

        return self.mlp(hidden_states),residual

class Qwen3Model(nn.Module):
    def __init__(self,config:Qwen3Config):
        super().__init__()

        self.norm = RMSNorm(config.hidden_size,eps=config.rms_norm_eps)

        self.layers = nn.ModuleList(
            [Qwen3DecoderLayer(config,i) for i in range(config.num_hidden_layers)]
        )
        self.embed_tokens = nn.Embedding(config.vocab_size,config.hidden_size)

        self.rotary_emd = RotaryEmbedding(
                    config.head_dim,
                    config.head_dim,
                    config.max_position_embeddings,
                    float(config.rope_parameters['rope_theta'])
                )
    def forward(self,input_ids:torch.Tensor,positions:torch.Tensor) -> torch.Tensor:
        hidden_states,residual = self.embed_tokens(input_ids),None
        for layer in self.layers:
            hidden_states,residual=layer(positions,hidden_states,self.rotary_emd,residual)

        return self.norm(hidden_states,residual)[0]

class Qwen3ForCausalLM(nn.Module):
    def __init__(self,config:Qwen3Config):
            super().__init__()

            self.model = Qwen3Model(config)

            # if getattr(config,"tie_word_embeddings",False):
            #    self.lm_head = _TiedLinear(self.model.embed_tokens.weight)
            # else:
            self.lm_head = nn.Linear(config.hidden_size,config.vocab_size,bias=False)
    def forward(self,input_ids:torch.Tensor,positions:torch.Tensor) -> torch.Tensor:
        return self.model(input_ids,positions)

    def compute_logits(self,hidden_states: torch.Tensor) -> torch.Tensor:
        return self.lm_head(hidden_states)

    def load_weights(self, weights: Iterable[tuple[str, torch.Tensor]]) -> None:
        # weights: (权重名, 权重张量) 序列；QKV 分片先合并，其余直接拷贝
        params = dict(self.named_parameters())
        loaded: set[str] = set()
        attn0 = self.model.layers[0].self_attn
        # HF 的 q/k/v 按以下偏移写入本项目融合后的 qkv_proj
        qkv_slices = {
            "q_proj": (0, attn0.q_size),
            "k_proj": (attn0.q_size, attn0.kv_size),
            "v_proj": (attn0.q_size + attn0.kv_size, attn0.kv_size),
        }
        shards_seen: dict[str, int] = {}

        for name, w in weights:
            stem = name.removesuffix(".weight")
            leaf = stem.rsplit(".", 1)[-1]  # 取末级名，如 q_proj
            if leaf in qkv_slices:
                fused = stem.rsplit(".", 1)[0] + ".qkv_proj.weight"
                off, size = qkv_slices[leaf]
                # 切片写入融合权重，copy_ 同时完成精度转换
                with torch.no_grad():
                    params[fused][off : off + size].copy_(w)
                shards_seen[fused] = shards_seen.get(fused, 0) + 1
            elif name in params:
                default_weight_loader(params[name], w)
                loaded.add(name)

        # QKV 三片全部到齐才算加载完成，否则留给下方缺失检查报错
        for fused, seen in shards_seen.items():
            if seen == len(qkv_slices):
                loaded.add(fused)

        if "lm_head.weight" not in loaded and "model.embed_tokens.weight" in loaded:
            # 权重共享：embed_tokens 与 lm_head 绑定，复用已加载的词嵌入
            default_weight_loader(
                params["lm_head.weight"], params["model.embed_tokens.weight"]
            )
            loaded.add("lm_head.weight")
        missing = set(params) - loaded
        if missing:
            preview = ", ".join(sorted(missing)[:8])
            raise ValueError(f"checkpoint is missing {len(missing)} model weights: {preview}")


if __name__ == "__main__":
    from transformers import AutoModelForCausalLM

    config = Qwen3Config(
        vocab_size=256,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        head_dim=16,
        max_position_embeddings=64,
        rms_norm_eps=1e-6,
        rope_theta=1_000_000.0,
        tie_word_embeddings=True,
    )

    ours = Qwen3ForCausalLM(config).eval()

    # 用同一份 config 构造官方模型，方便对比结构
    official = AutoModelForCausalLM.from_config(config).eval()

    print("=== 我们的模型 ===")
    print(ours)

    print("\n=== Hugging Face 官方模型 ===")
    print(official)

    input_ids = torch.randint(0, config.vocab_size, (1, 8))
    positions = torch.arange(8).unsqueeze(0)

    with torch.inference_mode():
        hidden = ours(input_ids, positions)
        logits = ours.compute_logits(hidden[:, -1])

    ours_params = sum(p.numel() for p in ours.parameters())
    official_params = sum(p.numel() for p in official.parameters())

    print(
        f"\n参数量: ours={ours_params:,}, "
        f"official={official_params:,}"
    )
