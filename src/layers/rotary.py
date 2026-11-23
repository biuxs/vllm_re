import torch
from torch import nn

class RotaryEmbedding(nn.Module):
    def __init__(self, head_size: int, rotary_dim: int, max_position_embedding: int, base: float = 1000):
        super().__init__()

        if rotary_dim % 2 != 0:
            raise ValueError("rotary_dim must be even!")

        if rotary_dim > head_size:
            raise ValueError("rotary_dim must be smaller than head_size!")

        self.head_size = head_size
        self.rotary_dim = rotary_dim
        self.half = rotary_dim // 2
        inv_freq = 1.0/(base ** (torch.arange(0,rotary_dim, step = 2, dtype=torch.float)/rotary_dim))
        t = torch.arange(max_position_embedding, dtype=torch.float)
        freqs = torch.outer(t, inv_freq)
        self.register_buffer(
            'cos_sin_cache',
            torch.cat((freqs.cos(),freqs.sin()),dim=-1),
        persistent=False,
        )

    def forward(self,positions:torch.Tensor,query:torch.Tensor,key:torch.Tensor) ->tuple[torch.Tensor,torch.Tensor]:
        flat = positions.reshape(-1)
        cos_sin = self.cos_sin_cache.index_select(0,flat).to(query.dtype)
        cos,sin = cos_sin.chunk(2,-1)
        cos = cos.unsqueeze(-2)
        sin = sin.unsqueeze(-2)

        def _apply(x: torch.Tensor):
            shape = x.shape
            x = x.reshape(flat.numel(),-1,self.head_size)
            out = torch.empty_like(x)

            x1,x2 = x[...,:self.half],x[...,self.half:self.rotary_dim]
            o1,o2 = out[...,:self.half],out[...,self.half:self.rotary_dim]

            tmp = torch.empty_like(o1)
            torch.mul(x1,cos,out=o1)
            torch.mul(x2,sin,out=tmp)
            o1.sub_(tmp)
            torch.mul(x1,sin,out=o2)
            torch.mul(x2,cos,out=tmp)
            o2.add_(tmp)

            if self.rotary_dim<self.head_size:
                out[...,self.rotary_dim:self.head_size].copy_(x[...,self.rotary_dim:self.head_size])
            return out.reshape(shape)
        return _apply(query),_apply(key)


if __name__ == "__main__":
    import time
    import torch
    from transformers import LlamaConfig
    from transformers.models.llama.modeling_llama import (
        LlamaRotaryEmbedding,
        apply_rotary_pos_emb,
    )

    torch.manual_seed(0)
    H, D, M, N, BASE = 32, 128, 131072, 4096, 1_000_000

    rope = RotaryEmbedding(D, D, M, BASE)
    hf = LlamaRotaryEmbedding(
        LlamaConfig(
            hidden_size=H * D,
            num_attention_heads=H,
            head_dim=D,
            max_position_embeddings=M,
            rope_theta=BASE,
        )
    )

    pos = torch.arange(N)
    q = k = torch.randn(1, N, H, D, dtype=torch.bfloat16)

    cos, sin = hf(q, pos.unsqueeze(0))
    k_hf = apply_rotary_pos_emb(q, k, cos, sin, 2)[1]
    k_ours = rope(pos, q, k)[1]

    print(
        "精度 逐 bit 相同:",
        torch.equal(k_hf, k_ours),
        " max|diff|:",
        (k_hf.float() - k_ours.float()).abs().max().item(),
    )

    def bench(fn, n=20):
        fn()
        t = time.perf_counter()
        for _ in range(n):
            fn()
        return (time.perf_counter() - t) / n * 1e3

    t_ours = bench(lambda: rope(pos, q, k))
    t_hf = bench(lambda: apply_rotary_pos_emb(q, k, cos, sin, 2))

    print(f"性能 ours {t_ours:.2f} ms | HF {t_hf:.2f} ms | {t_hf / t_ours:.2f}x")