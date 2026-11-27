import torch
from torch import nn
import torch.nn.functional as F
from  ..kv.context import get_forward_context

class Attention(nn.Module):
    def __init__(self,num_heads:int,head_dim:int,num_kv_heads:int,layer_id: int=0):
        super().__init__()
        if num_heads % num_kv_heads != 0:
            raise ValueError("num heads must be divisiable by mun_kv_heads!")

        self.num_heads = num_heads
        self.head_dim = head_dim
        self.num_kv_heads = num_kv_heads
        self.layer_id = layer_id
        self.groups = self.num_heads // num_kv_heads

        self.scaling = head_dim ** -0.5


        
    
    def _sdpa(
            self,
            query:torch.Tensor,
            key:torch.Tensor,
            value:torch.Tensor,
            is_causal: bool,
            attn_mask: torch.Tensor | None = None,) -> torch.Tensor:
        """输入 Q=[B,H,S,D]，K/V=[B,Hkv,T,D]，输出 [B,H,S,D]。"""
        B, H, S, D = query.shape
        T = key.shape[2]
        query = query.reshape(B,self.num_kv_heads,self.groups,S,D)
        key = key.unsqueeze(2)
        value = value.unsqueeze(2)

        # 半精度输入使用 FP32 计算分数、softmax 和加权求和，最后还原 dtype。
        compute_dtype = (
            torch.float32 if query.dtype in (torch.float16, torch.bfloat16) else query.dtype
        ) 
        #[B,Hkv,G,S,T]
        scores = query.to(compute_dtype) @ key.to(compute_dtype).transpose(-2,-1) *self.scaling
        if attn_mask is not None:
            # 兼容可广播到 [B,H,S,T] 的布尔 mask 和加性 mask。
            mask = torch.broadcast_to(attn_mask, (B, H, S, T)).reshape(
                B, self.num_kv_heads, self.groups, S, T
            )
            if attn_mask.dtype == torch.bool:
            # True 表示允许关注，False 表示禁止关注
                scores = scores.masked_fill(~mask, float("-inf")
            )
            else:
                scores = scores + mask.to(compute_dtype)
        elif is_causal:
            # Q 对应完整 KV 序列的最后 S 个位置
            q_pos = torch.arange(T - S, T, device=query.device)[:, None]
            kv_pos = torch.arange(T, device=query.device)[None, :]

            # 屏蔽未来位置
            mask = kv_pos > q_pos

            scores = scores.masked_fill(mask, float("-inf"))

        weights = torch.softmax(scores, dim=-1)
        # 一整行被 mask 时 softmax(-inf) 为 NaN，按 SDPA 约定输出零。
        weights = torch.nan_to_num(weights, nan=0.0)
        out = weights @ value.to(compute_dtype)
        return out.reshape(B, H, S, D).to(query.dtype)

    def forward(self, q: torch.Tensor, k: torch.Tensor, v: torch.Tensor) -> torch.Tensor:
        ctx = get_forward_context()

        q = q.transpose(1, 2)
        k = k.transpose(1, 2)
        v = v.transpose(1, 2)
        # 1 q.shape=[B,S,H,D],transpose用来交换中间两维 [B,H,S,D]
        # 2 SDPA要求H维度在前，内存连续，我们需要对每个头做矩阵乘法
        # 3 B batch_size 批次大小，一般指句子数量
        # 4 S seq_len 序列长度，一般指token数量
        # 5 H num_heads 头数量 D head_dim 单头维度


        if ctx is None:
        # 无 ForwardContext（如单独跑模型）：走无 KV Cache SDPA
            out = self._sdpa(q,k,v,is_causal=True)
            return out.transpose(1, 2).contiguous()
            # 1 换回维度，以便后续运算
            # 2 contiguous 保证内存连续 经过transpose后，内存存储是乱序的，如果非连续张量调用reshape会报错，因此需要内存连续
            # 3 具体做法是拷贝一份张量，在内存里重新排序，对齐逻辑顺序和内存顺序

        # 1) write 把本步计算出来的新 K、V 按 slot_mapping 写入 KV 缓存存储池
        ctx.store.write(self.layer_id,ctx.slot_mapping,k.transpose(1,2),v.transpose(1,2))

        if ctx.is_prefill:
            return self._prefill(q,k,v,ctx)
        return self._decode(q,ctx)

    def _prefill(self,q,k,v,ctx) ->torch.Tensor:
        cached = ctx.cached_lens[0] if ctx.cached_lens else 0
        ctx_len = ctx.context_lens[0]
        q_len = ctx.query_lens[0]

        if cached == 0:
            out = self._sdpa(q,k,v,is_causal=True)
            return out.transpose(1, 2).contiguous()

        k_full, v_full = ctx.store.gather(self.layer_id, ctx.block_tables[0], ctx_len)
        k_full = k_full.unsqueeze(0).transpose(1, 2)
        v_full = v_full.unsqueeze(0).transpose(1, 2)
        device = q.device
        qi = torch.arange(q_len,device=device).unsqueeze(1) + cached
        ki = torch.arange(ctx_len,device=device).unsqueeze(0)
        allow = qi >= ki
        out = self._sdpa(q, k_full, v_full, is_causal=False, attn_mask=allow)
        return out.transpose(1, 2).contiguous()

    def _decode(self,q,ctx) -> torch.Tensor:
        outs = []
        for b in range(q.shape[0]):
            ctx_len = ctx.context_lens[b]
            #返回[s,hkv,d]
            k_c,v_c = ctx.store.gather(self.layer_id, ctx.block_tables[b], ctx_len)
            k_c = k_c.unsqueeze(0).transpose(1,2)
            v_c = v_c.unsqueeze(0).transpose(1,2)
            outs.append(self._sdpa(q[b:b+1],k_c,v_c,is_causal=False))
        return torch.cat(outs, dim=0).transpose(1, 2).contiguous()

if __name__ == "__main__":
    torch.manual_seed(42)

    B,S,H,Hkv,D = 1,1024,16,8,128

    device ="cuda"

    q=torch.randn(B,S,H,D,device=device)
    k=torch.randn(B,S,Hkv,D,device=device)

    att = Attention(H,D,Hkv)

    out = att(q,k,k)

    reference = F.scaled_dot_product_attention(
        q.transpose(1,2),k.transpose(1,2),k.transpose(1,2),
        is_causal=True,scale=D** -0.5,enable_gqa=True,
    ).transpose(1,2)

    print(torch.allclose(out,reference,atol=1e-4,rtol=1e-3))
