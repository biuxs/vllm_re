tag v2 中，实现：

- **Continuous Batching**：每个推理 step 都重新选择当前参与计算的请求，已完成请求立即退出，新请求可以进入后续 step。
- **Chunked Prefill**：长 prompt 不要求一次算完，而是受每轮 token 预算限制，被拆成多个 chunk，KV Cache 在各轮之间保存进度。

## 1. 请求状态

每个请求用一个 `Sequence` 表示，关键状态在 [sequence.py](/D:/infra/dzyy-vllm-main/vllm-v2/src/kv/sequence.py:20)：

```python
status: SequenceStatus
num_cached_tokens: int
num_scheduled_tokens: int
is_prefill: bool
block_table: list[int]
```

含义如下：

| 字段 | 含义 |
|---|---|
| `num_cached_tokens` | 已经计算并写入 KV Cache 的 token 数 |
| `num_scheduled_tokens` | 当前 step 准备计算的 token 数 |
| `is_prefill` | 当前是 prefill 还是 decode |
| `block_table` | 该序列使用的物理 KV 块 |
| `status` | `WAITING`、`RUNNING` 或 `FINISHED` |

调度器维护两个队列：

```python
self.waiting: deque[Sequence]
self.running: deque[Sequence]
```

```text
waiting：
  新请求
  Prefill 尚未完成的请求
  被抢占后等待重新计算的请求

running：
  Prefill 已完成、正在逐 token decode 的请求
```

状态流可以概括为：

```text
add_request
    ↓
WAITING / waiting
    ↓ 多轮 chunked prefill
RUNNING / running
    ↓ 多轮 decode
FINISHED
    ↓
释放或保留 KV Cache
```

## 2. 每个 step 动态组 batch

`Engine.step()` 每轮都会重新调用 [scheduler.py](/D:/infra/dzyy-vllm-main/vllm-v2/src/engine/scheduler.py:32)：

```python
prefills, decodes = self.scheduler.schedule()
```

然后执行模型：

```python
p_ids, d_ids = self.model_runner.run(prefills, decodes)
```

最后更新请求状态：

```python
self.scheduler.postprocess(decodes, d_ids, is_prefill=False)
self.scheduler.postprocess(prefills, p_ids, is_prefill=True)
```

所以 batch 不是在请求到达时固定下来的，而是每个 step 重新形成：

```text
step 1: A decode、B decode、C prefill chunk
step 2: A decode、C prefill chunk
step 3: A decode、C decode、D prefill chunk
```

B 完成后立即退出；C 完成 prefill 后进入 decode；D 可以在后续 step 加入。这就是 Continuous Batching 的核心。

## 3. 两个调度约束

调度器同时限制序列数量和 token 数量：

```python
max_num_seqs
max_num_batched_tokens
```

`max_num_seqs` 限制本轮最多调度多少条序列：

```python
len(prefills) + len(decodes) < self.max_num_seqs
```

`max_num_batched_tokens` 限制本轮总共处理多少 token：

```text
本轮 token 数
= decode 序列数
+ 所有 prefill chunk 的 token 数
```

因为每条 decode 序列每轮只处理一个 token，所以一条 decode 消耗一个 token 预算。

## 4. 默认混合调度

当：

```python
mix_prefill_decode = True
```

会进入 [scheduler.py](/D:/infra/dzyy-vllm-main/vllm-v2/src/engine/scheduler.py:163) 的 `_schedule_mixed()`：

```python
used = self._schedule_decode(prefills, decodes, 0)

if decodes:
    self._pull_prefill_chunk(prefills, decodes, used)
    return prefills, decodes
```

策略是：

```text
1. 优先选择 running 中的 decode 请求
2. 每条 decode 消耗 1 个 token 预算
3. 如果还有剩余预算，从 waiting 队首取一个 prefill chunk
```

例如：

```text
max_num_batched_tokens = 8

running:
  A、B、C

waiting:
  D，尚有 20 个 prompt token 未计算
```

本轮调度结果：

```text
A decode：1 token
B decode：1 token
C decode：1 token
D prefill：5 token

总计：8 token
```

这样长 prompt D 不会一次占满整个计算步骤，已有请求 A、B、C 仍能持续输出 token，降低 decode 延迟。

## 5. Decode 如何形成动态 batch

`_schedule_decode()` 从 `running` 中依次取请求：

```python
seq = self.running.popleft()
seq.num_scheduled_tokens = 1
seq.is_prefill = False
decodes.append(seq)
```

选中的请求又被放回 `running`：

```python
self.running.extendleft(reversed(decodes))
```

因此未完成请求会在下一轮继续参与调度。

ModelRunner 将所有 decode 请求组成真正的张量 batch：

```python
input_ids = [seq.last_token for seq in seqs]
ids = self._tensor(input_ids).unsqueeze(-1)
```

形状是：

```text
[B_decode, 1]
```

Attention 再按 batch 中每条序列自己的 `block_table` 和 `context_len` 读取历史 KV：

```python
for b in range(q.shape[0]):
    k_c, v_c = ctx.store.gather(
        self.layer_id,
        ctx.block_tables[b],
        ctx.context_lens[b],
    )
```

## 6. Chunked Prefill 如何切分

核心代码在 [scheduler.py](/D:/infra/dzyy-vllm-main/vllm-v2/src/engine/scheduler.py:107)：

```python
need = seq.num_tokens - seq.num_cached_tokens
remaining = self.max_num_batched_tokens - tokens_used
chunk = min(need, remaining)

seq.num_scheduled_tokens = chunk
```

其中：

```text
need：
  该 prompt 还有多少 token 未计算

remaining：
  本 step 扣除 decode 后还剩多少 token 预算

chunk：
  本轮实际处理的 prompt 长度
```

例如：

```text
prompt 长度          = 20
num_cached_tokens    = 0
本轮剩余预算          = 6
```

则：

```python
chunk = min(20, 6) = 6
```

第一轮处理：

```text
token [0:6]
```

postprocess 后：

```text
num_cached_tokens = 6
```

下一轮再计算：

```text
need = 20 - 6 = 14
token [6:12]
```

如此继续，直到整个 prompt 完成。

## 7. ModelRunner 如何只计算当前 chunk

在 [model_runner.py](/D:/infra/dzyy-vllm-main/vllm-v2/src/engine/model_runner.py:50)：

```python
start = seq.num_cached_tokens
end = start + seq.num_scheduled_tokens

tokens = seq.token_ids[start:end]
positions = list(range(start, end))
```

所以送入模型的只包含当前 chunk：

```text
input_ids.shape = [1, end-start]
Q/K/V 的新计算长度 = end-start
```

它还会建立本轮上下文：

```python
ForwardContext(
    is_prefill=True,
    block_tables=[seq.block_table],
    slot_mapping=slots,
    query_lens=[end - start],
    context_lens=[end],
    cached_lens=[start],
)
```

这些字段分别表示：

```text
cached_lens = start：
  前面已有多少 KV

query_lens = end - start：
  当前 chunk 的 Q 长度

context_lens = end：
  历史 KV + 当前 chunk 的完整上下文长度
```

## 8. Chunk 之间如何接续 Attention

每一层 Attention 先把本轮新 K/V 写入缓存：

```python
store.write(
    layer_id,
    ctx.slot_mapping,
    k,
    v,
)
```

如果这是第一个 chunk：

```python
cached == 0
```

直接使用当前 `q/k/v` 做 causal attention：

```python
self._sdpa(q, k, v, is_causal=True)
```

如果是后续 chunk，则读取从位置 `0` 到 `end` 的完整 K/V：

```python
k_full, v_full = ctx.store.gather(
    self.layer_id,
    ctx.block_tables[0],
    ctx_len,
)
```

形状关系为：

```text
Q 长度      = 当前 chunk 长度
K/V 长度    = 历史长度 + 当前 chunk 长度
```

例如第二个 chunk：

```text
cached = 6
q_len = 6
context_len = 12

Q 对应位置：     6...11
K/V 对应位置：   0...11
```

由于当前 chunk 内也有多个 token，必须构造绝对位置的 causal mask：

```python
qi = arange(q_len) + cached
kj = arange(context_len)
allow = qi >= kj
```

得到：

```text
Q位置6  可以看 K位置0...6
Q位置7  可以看 K位置0...7
...
Q位置11 可以看 K位置0...11
```

## 9. 每轮结束后推进进度

在 [scheduler.py](/D:/infra/dzyy-vllm-main/vllm-v2/src/engine/scheduler.py:194)：

```python
seq.num_cached_tokens += seq.num_scheduled_tokens
seq.num_scheduled_tokens = 0
```

如果 prompt 还没完成：

```python
if is_prefill and seq.num_cached_tokens < seq.num_prompt_tokens:
    continue
```

请求继续留在 `waiting`，下一轮从新的 `num_cached_tokens` 接着算。

只有最后一个 prefill chunk 完成后，模型产生的 token 才会真正追加到序列：

```python
seq.append_token(token_id)
```

随后该请求进入正常 decode。

当前实现有一个小的性能问题：中间 chunk 也会计算 logits 并采样，但 `postprocess()` 会丢弃结果。正确性不受影响，只是有少量无效计算。

## 10. Prefill-first 模式

当：

```python
mix_prefill_decode = False
```

调度器使用 `_schedule_prefill_first()`：

```text
有 waiting prefill：
  只调度 prefill

没有 prefill：
  才调度 decode
```

这种模式也支持 chunked prefill，但长 prompt 可能连续多个 step 占用 prefill 预算，正在生成的请求会暂停输出，因此 decode 延迟更高。

默认混合模式则优先保证 decode：

```text
先 decode
再用剩余预算做一个 prefill chunk
```

## 11. KV 不足时的抢占

decode 追加 token 可能需要新 KV 块：

```python
while not self.kv.can_append(seq):
    self.preempt(...)
```

抢占逻辑：

```python
seq.status = WAITING
self.kv.deallocate(seq, publish=False)
self.waiting.appendleft(seq)
```

也就是：

```text
RUNNING
  ↓ KV 空间不足
释放该请求 KV
  ↓
重新回到 WAITING
  ↓
后续重新 prefill
```

这是 `recompute` 式抢占。当前实现会丢掉该请求已经计算的 KV，而不是 swap 到 CPU，因此简单但重算成本较高。

## 总结

整个机制可以压缩成下面这条流程：

```text
每个 Engine.step()
    │
    ├─ 从 running 动态选择多个 decode
    │     每条消耗 1 token 预算
    │
    ├─ 使用剩余预算选择一个 prefill chunk
    │     chunk = min(未缓存长度, 剩余预算)
    │
    ├─ decode 组成 [B, 1] 批量执行
    │
    ├─ prefill chunk 逐条执行
    │     只计算新 token 的 Q/K/V
    │     历史 K/V 从分页缓存读取
    │
    └─ 更新状态
          未完成 prefill → 留在 waiting
          prefill 完成   → 转入 running
          decode 未完成  → 留在 running
          请求完成       → 释放 KV
```

因此，Continuous Batching 主要体现在“每轮动态更新请求集合”，Chunked Prefill 则通过 `num_cached_tokens + num_scheduled_tokens + token budget` 将长 prompt 跨多个 step 增量执行。
