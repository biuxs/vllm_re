import torch
from torch import nn
import torch.nn.functional as F


class Attention(nn.Module):
    def __init__(self,num_heads:int,head_dim:int,num_kv_heads:int):
        super().__init__()
        if num_heads % num_kv_heads != 0:
            raise ValueError("num heads must be divisiable by mun_kv_heads!")

        self.num_heads = num_heads
        self.head_dim = head_dim
        self.num_kv_heads = num_kv_heads

        self.groups = self.num_heads // num_kv_heads

        self.scale = head_dim ** -0.5

    def forward(self,query:torch.Tensor,key:torch.Tensor,value:torch.Tensor) -> torch.Tensor:
        B, S, H, D = query.shape
        query = query.transpose(1,2).reshape(B,self.num_kv_heads,self.groups,S,D)
        key = key.transpose(1,2).unsqueeze(2)
        value = value.transpose(1,2).unsqueeze(2)

        mask = ~torch.ones(S,S,dtype=torch.bool,device=query.device).tril()
        scores = query @ key.transpose(-2,-1) *self.scale
        scores = scores.masked_fill(mask,float("-inf"))

        out = F.softmax(scores,dim=-1,dtype=torch.float32).to(query.dtype) @value

        return out.reshape(B,H,S,D).transpose(1,2)

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
