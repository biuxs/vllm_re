from __future__ import annotations
import torch



class Sampler:
    def __call__(
            self,
            logits: torch.Tensor,
            temperatures:torch.Tensor | None = None,
            top_ps: torch.Tensor | None = None,
            top_ks: torch.Tensor | None = None,
            min_ps: torch.Tensor | None = None,
                 ) -> torch.Tensor:

        if temperatures is None and top_ks is None and top_ps is None:
            return torch.argmax(logits,dim=-1)

        if temperatures is not None and torch.all(temperatures==0):
            return torch.argmax(logits, dim=-1)

        logits = logits.float()

        if temperatures is not None:
            temperatures = torch.where(temperatures <=0,1.0,temperatures)
            logits  = logits/temperatures.unsqueeze(-1)

        logits = _top_k_top_p(logits, top_ps, top_ks)
        logits = _min_p(logits, min_ps)
        probs = torch.softmax(logits, dim=-1)
        return torch.multinomial(probs, num_samples=1).squeeze(-1)

def _top_k_top_p(
    logits:torch.Tensor,
    top_p:torch.Tensor | None = None,
    top_k:torch.Tensor | None = None,
) -> torch.Tensor:
        if top_p is None and top_k is None:
            return logits
        vocab_size = logits.size(-1)
        sorted_logits,sorted_idx = logits.sort(dim=-1,descending=True)
        if top_k is not None:
            sorted_logits = _apply_top_k(sorted_logits, top_k, vocab_size)

        if top_p is not None:
            sorted_logits = _apply_top_p(sorted_logits, top_p)

        return torch.empty_like(sorted_logits).scatter(1,sorted_idx,sorted_logits)

def _apply_top_k(sorted_logits: torch.Tensor, top_k: torch.Tensor,vocab_size: int) -> torch.Tensor:
    # k<=0 视为不限制；k>vocab_size 截断到 vocab_size
    k = torch.where(top_k <= 0, vocab_size, top_k)
    k = torch.clamp(k,max = vocab_size).to(torch.long)
    cutoff = sorted_logits.gather(1, (k - 1).unsqueeze(1))
    min_val = torch.finfo(sorted_logits.dtype).min
    return sorted_logits.masked_fill(sorted_logits < cutoff,min_val)


def _apply_top_p(sorted_logits: torch.Tensor, top_p: torch.Tensor) -> torch.Tensor:
    p = torch.where(top_p <= 0, 1.0, top_p)

    probs = sorted_logits.softmax(dim=-1)

    mask = probs.cumsum(dim=-1) - probs> p.unsqueeze(1)

    # 第一个位置（最大概率）必须保留
    mask[:, 0] = False
    # 将超出 p 的部分置为 -inf
    min_val = torch.finfo(sorted_logits.dtype).min
    return sorted_logits.masked_fill(mask, min_val)
def _min_p(logits: torch.Tensor, min_p: torch.Tensor | None) -> torch.Tensor:
    if min_p is None:
        return logits
    probs = torch.softmax(logits, dim=-1)
    top, _ = probs.max(dim=-1, keepdim=True)
    return logits.masked_fill(probs < min_p.unsqueeze(1) * top, -float("inf"))