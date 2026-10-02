# Configuration A: two-worker default routing

This historical pre-orchestrator run uses one Ray/vLLM replica on each of two A100 40GB worker nodes. The Ray head and GraphServe CPU services run on a third control node.

- Ray Serve LLM: 2.58.0
- vLLM: bundled 0.26.0
- Model: Qwen/Qwen2.5-Coder-7B-Instruct, BF16
- Replicas: 2, one whole GPU each on distinct Kubernetes nodes
- Maximum model length: 8,192
- Maximum sequences per replica: 8
- GPU memory utilization: 0.9
- Automatic prefix caching: disabled
- Request routing: Ray default power-of-two choices

All 36 measured requests succeeded:

| Concurrency | Throughput (requests/s) | p50 latency (s) | p95 latency (s) | Total tokens/s |
|---:|---:|---:|---:|---:|
| 1 | 0.138 | 6.38 | 11.13 | 738 |
| 2 | 0.285 | 6.46 | 10.73 | 1,524 |
| 4 | 0.492 | 6.62 | 12.06 | 2,633 |

This is the historical comparison baseline for the two-worker prefix-aware profile. The application remains non-streaming, so these artifacts do not establish per-request TTFT.
