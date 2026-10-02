# GraphServe serving design

## Workload and request path

GraphServe is a repository-understanding RAG application. It retrieves
Graphify-connected source excerpts, optionally reranks them with SIE, assembles
a bounded cited prompt, invokes Qwen, and verifies every returned source ID and
immutable Git blob.

```text
browser/API -> agent retrieval -> SIE -> context assembly
            -> nginx -> guard -> admission -> explicit placement
            -> bounded worker queue -> stable Ray Serve/vLLM target
            -> source verification
```

The inference gateway and engines are separate deployments. vLLM retains
ownership of continuous batching, scheduling, KV block tables and kernels.

## Measured topology

The cluster contains one K3s control node and two GPU workers. Each worker has
one A100-SXM4-40GB and runs one full Qwen replica. Pod anti-affinity prevents
both model workers from landing on the same node. Model weights are cached on
the shared Lambda filesystem, while each replica owns its GPU KV cache.

The measured model is `Qwen/Qwen2.5-Coder-7B-Instruct` at cached revision
`c03e6d358207e414f1eca0bb1891e29f1db0e242`, BF16, tensor parallelism 1, with
an 8,192-token serving limit.

## KV capacity estimate

The model has 28 layers, hidden size 3,584, 28 attention heads and four KV
heads. Head dimension is 128. Approximate unquantized BF16 KV bytes per token:

```text
2 (K,V) × 28 layers × 4 KV heads × 128 × 2 bytes = 57,344 bytes = 56 KiB
```

One full 8,192-token sequence therefore needs about 448 MiB of KV before
allocator overhead; eight need about 3.5 GiB. vLLM startup reported roughly
20 GiB of KV space per replica, so configured sequence count is plausible, but
the safe operating limit must be established through latency and queue behavior.

## Measured profiles

Both profiles use `max_num_seqs=8`, `gpu_memory_utilization=0.9`, BF16 and two
whole-GPU replicas.

- **A:** automatic prefix caching disabled; Ray default routing.
- **B:** automatic prefix caching enabled; Ray prefix-affinity routing.

| Concurrency | A req/s | B req/s | A p95 | B p95 | A success | B success |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.138 | 0.143 | 11.13 s | 10.75 s | 12/12 | 12/12 |
| 2 | 0.285 | 0.272 | 10.73 s | 13.41 s | 12/12 | 12/12 |
| 4 | 0.492 | 0.038 | 12.06 s | 80.92 s | 12/12 | 8/12 |

Profile B's concurrency-4 regression means the default profile remains active.
Because B changed both vLLM caching and routing, the result does not identify a
single cause. A clean follow-up enables automatic prefix caching while retaining
default routing, with prefix affinity as an optional third profile.

## Gateway control boundary

The gateway has a named guard for request shape and token bounds. Admission uses
a per-tenant one-minute token window, deadline/queue estimates, worker health,
and per-worker vLLM KV telemetry. Placement uses prefix affinity while queue
depth, in-flight work, engine waiting requests, and KV occupancy remain scoring
inputs. Each stable worker endpoint has a bounded priority queue; interactive
traffic precedes batch traffic. A request is never bounced after placement.

The affinity table is bounded and expires entries. Staying on the mapped worker
is recorded as `same_worker`; moving elsewhere is `cold_recompute`. No KV tensor
transfer is claimed. Engine 503/529 responses follow the declared `stay` policy,
while 429 remains local. vLLM retains scheduling, continuous batching, KV block
allocation, waiting and preemption.

## Scope decisions

Cross-node KV transfer, LMCache, llm-d, Mooncake and prefill/decode
disaggregation are intentionally excluded. A route change is a cold
recomputation and must be reported that way. SIE is used for retrieval
embeddings; it is not an inference configuration.
