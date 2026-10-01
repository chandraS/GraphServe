# Live stress testing

`scripts/stress_test.py` sends repository questions through `/questions`, opens
temporary SSH tunnels to GraphServe and Prometheus, reads the ignored API-key
file, and saves raw JSONL, summaries, and exact-range Prometheus data.

```bash
.venv/bin/python scripts/stress_test.py \
  --host CONTROL_PUBLIC_IP \
  --repository-id REPOSITORY_ID \
  --scope class9 \
  --concurrency 1,2,4 \
  --requests-per-phase 12 \
  --warmup 2 \
  --output-dir benchmarks/NEW_RUN
```

Cold model startup is excluded. A measured run must include warmups and last
long enough to span multiple Prometheus scrapes. Overload tests belong in a
separate run because expected failures must not be mixed into successful-request
latency percentiles.

## Recorded configurations

Both runs used Ray 2.58.0, bundled vLLM 0.26.0, two A100-SXM4-40GB workers,
Qwen2.5-Coder-7B-Instruct in BF16, an 8,192-token model limit, eight maximum
sequences per replica, and 90% GPU memory utilization.

- **A — `config-a-multinode-default`**: automatic prefix caching disabled;
  Ray default routing.
- **B — `config-b-multinode-prefix-aware`**: automatic prefix caching enabled;
  Ray `PrefixCacheAffinityRouter` with imbalance threshold 2.

| Concurrency | A success | B success | A requests/s | B requests/s | A p95 | B p95 |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 12/12 | 12/12 | 0.138 | 0.143 | 11.13 s | 10.75 s |
| 2 | 12/12 | 12/12 | 0.285 | 0.272 | 10.73 s | 13.41 s |
| 4 | 12/12 | 8/12 | 0.492 | 0.038 | 12.06 s | 80.92 s |

At concurrency 4, B returned four HTTP 502 responses after the agent's upstream
inference timeout. Ray remained healthy and recorded client cancellation. The
result is retained as measured evidence. It does not isolate whether automatic
prefix caching, routing, their interaction, or run variance caused the failure.

## Evidence layout

Each configuration directory contains:

- `requests.jsonl`: warmup and measured request records, including failures.
- `summary.json`: successful-request latency, throughput, status and token totals.
- `metrics-c*.json`: raw Prometheus ranges for each concurrency phase.
- `rayservice.yaml`: the exact serving manifest.
- `environment.txt`: node, pod, image, driver and Kubernetes evidence.
- `README.md`: concise interpretation.

`submission.ipynb` loads these files directly. It does not embed hand-entered
benchmark values. The application is currently non-streaming, so Prometheus
TTFT is aggregate and the notebook does not claim per-request TTFT or inter-token
latency.

## Still-needed experiments

1. Repeat runs and report variation or confidence intervals.
2. Separate identical, shared-prefix, partial-prefix and unique-prefix traffic.
3. Add mixed tenant/priority and overload phases.
4. Record worker identity, route reason and queue depth per request.
5. Add streaming first-token timestamps and held-out answer/citation scoring.
