# Configuration B: two-worker prefix-cache affinity

This run uses the same three-node cluster, model, resource limits, prompt workload,
and concurrency phases as Configuration A. It changes the serving profile by
enabling vLLM automatic prefix caching and Ray Serve's
`PrefixCacheAffinityRouter`.

- Ray Serve LLM: 2.58.0
- vLLM: bundled 0.26.0
- Model: Qwen/Qwen2.5-Coder-7B-Instruct, BF16
- Replicas: 2, one whole GPU each on distinct Kubernetes nodes
- Maximum model length: 8,192
- Maximum sequences per replica: 8
- GPU memory utilization: 0.9
- Automatic prefix caching: enabled
- Request routing: `PrefixCacheAffinityRouter`
- Router imbalance threshold: 2
- Router prefix match threshold: 0.1

The measured results were:

| Concurrency | Success | Throughput (requests/s) | p50 latency (s) | p95 latency (s) | Total tokens/s |
|---:|---:|---:|---:|---:|---:|
| 1 | 12/12 | 0.143 | 6.15 | 10.75 | 765 |
| 2 | 12/12 | 0.272 | 5.78 | 13.41 | 1,459 |
| 4 | 8/12 | 0.038 | 43.60 | 80.92 | 199 |

At concurrency 4, four requests exceeded the agent's 120-second inference
timeout and returned HTTP 502. Ray Serve remained healthy; its proxy logs recorded
the corresponding client disconnects and request cancellations. This run shows
that this prefix-affinity profile is not suitable as the default under the tested
settings. It does not by itself identify whether automatic prefix caching, router
parameters, their interaction, or run-to-run variance caused the regression.

The application remains non-streaming, so these artifacts do not establish
per-request TTFT.
