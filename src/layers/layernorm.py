import torch
from torch import nn

class RMSNorm(nn.Module):
    def __init__(self, hidden_size: int, eps: float = 1e-6):
        super().__init__()

        self.weight = nn.Parameter(torch.ones(hidden_size))
        self.eps = eps

    def forward(self, x: torch.Tensor, residual: torch.Tensor | None=None) -> torch.Tensor | tuple[torch.Tensor , torch.Tensor]:
        original_type = x.dtype

        x32 = x.float() # 因为涉及到开根号所以我们使用更高的精度fp32

        if residual is not None:
            x32 = x32 + residual
            residual = x32.to(original_type)

        rms = torch.rsqrt(x32.pow(2).mean(-1,keepdim=True) + self.eps)

        x_normed = x32 * rms
        out32 = x_normed * self.weight

        out = out32.to(original_type)

        return (out,residual) if residual is not None else out

if __name__ == "__main__":
    official = nn.RMSNorm(1024,eps=1e-6)
    my = RMSNorm(1024)

    x = torch.randn(8, 128, 1024)

    with torch.no_grad():
        official.weight.copy_(my.weight)

    print(official(x)[0,0,:10])
    print(my(x)[0,0,:10])

    print(torch.allclose(official(x), my(x)))