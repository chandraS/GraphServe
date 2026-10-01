# Configuration A baseline — 2026-09-29

This is a warm, application-level baseline through `/questions`: Graphify retrieval, SIE reranking, token-aware context assembly, nginx, the inference gateway, Ray Serve, vLLM, and citation verification. It is one of the two real vLLM configurations required by the assignment; configuration B has not yet been run.

## Frozen configuration

- GPU: NVIDIA A100-SXM4-40GB
- Model: `Qwen/Qwen2.5-Coder-7B-Instruct`, BF16, revision `c03e6d358207e414f1eca0bb1891e29f1db0e242`
- vLLM: max model length 8,192; max sequences 8; GPU memory utilization 0.9; prefix caching disabled
- Topology: one Ray GPU worker and one model replica
- Workload: 2 warmups, then 12 repository questions at each concurrency 1, 2, and 4

## Application results

| Concurrency | Success | Throughput req/s | Latency p50 | Latency p95 | Latency max | Accounted tokens/s |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 12/12 | 0.179 | 4.63 s | 9.73 s | 9.84 s | 959 |
| 2 | 12/12 | 0.328 | 5.19 s | 10.83 s | 11.05 s | 1,751 |
| 4 | 12/12 | 0.506 | 5.87 s | 13.68 s | 16.83 s | 2,713 |

Six of 36 measured responses needed the bounded model-output correction attempt. All corrected successfully; no response needed the conservative item-filtering fallback. Token totals include both inference attempts.

## Engine observations

Prometheus histogram quantiles use one-minute rates. Values below are the median sample within each exact phase; the final column gives the observed phase maximum where useful.

| Concurrency | TTFT p95 median | TTFT p95 max | vLLM E2E p95 median | TPOT p95 median | Running max | Waiting max | KV usage max | GPU util max |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.488 s | 0.488 s | 8.88 s | 0.0243 s | 1 | 0 | 0.84% | 92% |
| 2 | 0.488 s | 0.488 s | 8.25 s | 0.0243 s | 2 | 0 | 2.55% | 92% |
| 4 | 0.486 s | 0.919 s | 8.00 s | 0.0243 s | 4 | 0 | 4.58% | 91% |

Ray Serve queue depth stayed zero. The engine did not reach its waiting queue or KV limit, so this run supports the hypothesis that application/model compute and the gateway cap bind before KV capacity for this request mix. A separate overload run is still needed to find the actual knee.

## Limits of this evidence

This baseline does not satisfy the two-worker, explicit guard/admit/place/queue/hop, overflow, second-vLLM-configuration, streaming per-request TTFT, notebook, or PDF requirements. The source artifacts are preserved here so later runs can be compared without relying on Grafana screenshots.
