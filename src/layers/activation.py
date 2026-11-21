import torch
from torch import nn
import torch.nn.functional as F

class SiluAndMul(nn.Module):

    def forward(self,gate:torch.Tensor,up:torch.Tensor):
        return F.silu(gate)*up
if __name__ == "__main__":
    mod = SiluAndMul()
    gate = torch.randn(2, 10, 128)
    up = torch.randn(2, 10, 128)
    out = mod(gate, up)
    print("out shape", out.shape)