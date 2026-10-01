# Validation record

## Local validation

- Python 3.12.3 environment with dependencies pinned in `requirements.lock`.
- Ten pytest tests cover URL/SHA validation, Graphify extraction, immutable blob
  reads, traversal, symlink rejection, citation verification, token budgets,
  authentication, SIE retry/order behavior, directory scope, UI behavior, and
  invalid-model-output repair.
- Docker Compose and Kubernetes manifests render successfully.
- Deployment scripts pass shell syntax checks; saved benchmark JSON and YAML
  parse successfully.

## Three-node Lambda validation — 2026-10-01 UTC

- Three Ubuntu 22.04 nodes ran K3s v1.34.11+k3s1.
- Control node: Ray head and GraphServe CPU services.
- Two worker nodes: one NVIDIA A100-SXM4-40GB each, driver 580.105.08.
- KubeRay 1.7.0 managed Ray 2.58.0 using
  `rayproject/ray-llm:2.58.0-py312-cu130`; bundled vLLM was 0.26.0.
- Pod anti-affinity placed one whole-GPU model replica on each worker.
- `/v1/models` reported `qwen-coder` with an 8,192-token request limit.
- SIE returned 384-dimensional MiniLM embeddings from the CPU deployment.
- Agent, gateway, nginx, SIE, Ray head, both model workers, Prometheus and
  Grafana were healthy.
- A final end-to-end repository question succeeded after restoring profile A.

## Measured validation

Configuration A completed 36/36 measured requests. Throughput at concurrency
1, 2 and 4 was 0.138, 0.285 and 0.492 requests/s; p95 latency was 11.13, 10.73
and 12.06 seconds.

Configuration B completed 32/36. Its concurrency-4 phase returned four HTTP
502 responses after upstream inference timeouts; successful-request p95 was
80.92 seconds and throughput was 0.038 requests/s. These failures remain in the
raw JSONL and summary.

Prometheus evidence includes aggregate TTFT, E2E latency, request/token rates,
running/waiting requests, KV usage, Ray queue depth, GPU utilization and GPU
memory. The image did not expose a usable time-per-output-token series in these
snapshots.

## Open validation gaps

- No streaming per-request TTFT or inter-token latency.
- No repeated-run variance or isolated prefix workload.
- No explicit overload knee or tenant fairness experiment.
- No held-out semantic answer/citation quality score.
- No final exported PDF.
