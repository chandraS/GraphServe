# Multi-node vLLM configuration comparison

Both configurations used the same three-node K3s cluster, two A100 40GB model
workers, Qwen2.5-Coder-7B-Instruct in BF16, repository snapshot, question set,
warmup count, and concurrency phases. Each measured phase sent 12 requests.

| Concurrency | A success | B success | A req/s | B req/s | Throughput delta | A p50 (s) | B p50 (s) | A p95 (s) | B p95 (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 12/12 | 12/12 | 0.138 | 0.143 | +3.6% | 6.38 | 6.15 | 11.13 | 10.75 |
| 2 | 12/12 | 12/12 | 0.285 | 0.272 | -4.6% | 6.46 | 5.78 | 10.73 | 13.41 |
| 4 | 12/12 | 8/12 | 0.492 | 0.038 | -92.4% | 6.62 | 43.60 | 12.06 | 80.92 |

Configuration A uses Ray's default routing with vLLM automatic prefix caching
disabled. Configuration B enables automatic prefix caching and uses Ray Serve's
`PrefixCacheAffinityRouter`.

Configuration A is the current recommendation. It completed all 36 measured
requests and scaled throughput through concurrency 4. Configuration B completed
the low-concurrency phases but produced four agent inference timeouts at
concurrency 4. A repeated run and narrower experiments that change only one
parameter at a time are needed before attributing the regression to a specific
component.

These are end-to-end, non-streaming application measurements. Prompt and
completion token counts are captured from model responses, but per-request TTFT
is not available in this harness.
