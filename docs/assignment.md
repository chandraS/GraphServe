# Deferred measured assignment

The application scaffold is not the final assignment submission. Model benchmarking is intentionally deferred until a GPU environment is available.

Required next milestones:
- Freeze repository commit, prompt corpus, model and tokenizer revisions, image digests, Ray/vLLM/KubeRay versions, GPU/driver details, and context/output limits.
- Run **two real vLLM configurations sequentially** on the same hardware and model. Baseline lives in `deploy/serve.yaml` (BF16, TP=1, 8192 context, max_num_seqs=8, prefix cache disabled). Choose a second explicit serving configuration after baseline health, for example a controlled batch/scheduler setting change; save both effective engine configurations. Do not call the mock mode a configuration or fabricate measurements.
- Collect raw request-level timestamps, successful/failed requests, input/output token counts, TTFT and inter-token latency from a streaming measurement client, throughput, p50/p95 latency, concurrency, warmup policy, GPU memory and utilization. Separate retrieval/embedding latency from serving latency. Record repetitions and compare citation correctness/answer quality on held-out repository questions.
- Store raw results and provenance under `artifacts/`. Produce `submission.ipynb` with reproducible analysis and plots; execute it against those actual records and export a reviewed PDF. Neither a final notebook nor PDF is claimed complete before measurements.

LMCache, llm-d, prefill/decode disaggregation and additional model replicas are deferred. SIE embeddings do not replace the required two Qwen/vLLM serving configurations.
