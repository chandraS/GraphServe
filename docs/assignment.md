# Assignment progress

The repository now contains a real three-node deployment and two measured
vLLM serving profiles. The mock local mode is used only for development tests.

## Completed

- Commit-pinned public GitHub ingestion with Graphify extraction.
- Graph traversal, immutable Git-blob citations, token-aware context assembly,
  optional SIE reranking, and fail-closed source verification.
- K3s control plane plus two independent A100 GPU workers.
- KubeRay/Ray Serve LLM/vLLM deployment with one full replica per worker.
- Configuration A and B raw requests, summaries, exact manifests, environment
  evidence, and Prometheus ranges.
- Prometheus/Grafana application, serving, GPU, queue and KV metrics.
- Reproducible comparison notebook at `submission.ipynb`.

## Measured result

Configuration A completed all 36 measured requests and scaled to 0.492
requests/s at concurrency 4. Configuration B completed 32 of 36; four
concurrency-4 calls timed out, reducing successful throughput to 0.038
requests/s for that phase. The stable A profile was restored after measurement.

## Required before final PDF

- Add streaming request measurement for per-request TTFT and output-token timing.
- Add repeated, prefix-isolated, mixed-priority and overload experiments.
- Add a frozen held-out repository QA/citation-quality set.
- Implement and measure explicit guard/admit/place/queue controls if required by
  the serving rubric.
- Pin image digests and the Qwen revision in the deployment configuration.
- Execute the final notebook after those records are added and export/review PDF.

LMCache, llm-d, prefill/decode disaggregation and cross-node KV transfer remain
deferred. SIE embeddings are retrieval support and do not count as a Qwen/vLLM
configuration.
