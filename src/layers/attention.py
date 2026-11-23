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
        kwargs: dict = {"scale":self.scaling}
       

        B, H, S, D = query.shape
        T = key.shape[2]
        query = query.reshape(B,self.num_kv_heads,self.groups,S,D)
        key = key.unsqueeze(2)
        value = value.unsqueeze(2)

        
        scores = query @ key.transpose(-2,-1) *self.scaling
        if attn_mask is not None:
            if attn_mask.dtype == torch.bool:
            # True 表示允许关注，False 表示禁止关注
                scores = scores.masked_fill(~attn_mask, float("-inf")
            )
            else:
                scores = scores + attn_mask
        elif is_causal:
            # Q 对应完整 KV 序列的最后 S 个位置
            q_pos = torch.arange(T - S, T, device=query.device)[:, None]
            kv_pos = torch.arange(T, device=query.device)[None, :]

            # 屏蔽未来位置
            mask = kv_pos > q_pos

            scores = scores.masked_fill(mask, float("-inf"))

        out = F.softmax(scores,dim=-1,dtype=torch.float32).to(query.dtype) @value

        return out.reshape(B,H,S,D)

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

        start = ctx.cache_len   # 本步开始前已有 KV 长度
        n = q.shape[2]          # 本步新增 token 数
        total = ctx.seq_len     # 本步写完后的总 KV 长度
        assert total == start + n

        # 1) write 把本步计算出来的新 K、V 按 slot_mapping 写入 KV 缓存存储池
        ctx.store.write(self.layer_id,ctx.slot_mapping,k.transpose(1,2),v.transpose(1,2))

        # 2) gather 按 block_table 读回逻辑长度 total 的全部历史 K V
        k_c,v_c = ctx.store.gather(self.layer_id,ctx.block_table,total)
        # k_c.shape=[seq_len,num_kv_heads,head_dim]
        k_c = k_c.unsqueeze(0).transpose(1, 2)
        v_c = v_c.unsqueeze(0).transpose(1, 2)
        # k_c.shape=[1,num_kv_heads,seq_len,head_dim]

        # 3) attend：本步 Q 与历史做注意力，掩码随 Prefill / Decode / 部分 Prefill 变化

        if n == 1: # Decode 只看历史，无掩码
            out = self._sdpa(q,k_c,v_c,is_causal=False)
        else: out = self._sdpa(q,k_c,v_c,is_causal=True)

        return out.transpose(1, 2).contiguous()

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
