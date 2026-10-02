# Serving requirements audit

This audit describes the measured three-node GraphServe deployment. The `class9`
repository is the analysis corpus; code found inside that repository is evidence
for answers and is not part of GraphServe's live serving path.

| Requirement | Status | Current evidence | Remaining work |
|---|---|---|---|
| Real repository-understanding application | Met | The Python agent ingests a commit-pinned repository, traverses Graphify relationships, reranks excerpts with SIE, assembles a bounded prompt, and verifies returned source IDs and Git blobs. | Add a frozen held-out quality set and semantic citation scoring. |
| Application uses the serving path | Met | Measured traffic used `/questions` and traversed agent → nginx → inference gateway → Ray Serve → vLLM. | Keep direct `/v1/models` and completion calls limited to health checks. |
| Gateway and engine are separate | Met | The Python gateway and Ray/vLLM run as separate Kubernetes deployments. | Preserve this boundary. |
| At least two model workers | Met | Configuration A and B each ran two whole-GPU replicas on two distinct A100 worker nodes; pod placement and environments are saved under `benchmarks/`. | Add per-worker request identity to benchmark records. |
| Two real vLLM configurations | Met | A disabled automatic prefix caching and used default routing. B enabled automatic prefix caching and `PrefixCacheAffinityRouter`. Both were measured with the same model, hardware, and request schedule. | Repeat a single-variable configuration if causal attribution is required. |
| Live serving metrics | Met | Prometheus snapshots include Ray/vLLM TTFT, E2E latency, tokens, running/waiting requests, KV usage, Ray queues, GPU utilization and GPU memory. | Add missing time-per-output-token series if the image exposes it. |
| Engine stays private | Met | Ray, Prometheus and Grafana are cluster-internal or VM-loopback and accessed with SSH tunnels. | Preserve this topology or add authenticated TLS ingress. |
| Engine scheduler is not reimplemented | Met | vLLM owns batching, waiting, KV block tables, preemption and kernels. | Gateway controls must stop at guard/admit/place/queue. |
| Explicit guard before GPU work | Met | The named gateway guard rejects body, JSON, model, stream, message, tool, output and context-token violations before admission; `orch_guard_decisions_total` records reasons and direct tests cover rejection. | Add policy tests when new request shapes are supported. |
| Admission control | Met | Admission uses a tenant token window, deadline/queue estimates, worker health and per-worker KV telemetry when samples are available; its own queue and in-flight state remain available at all times. It returns typed 429/503 reasons and `Retry-After`. | Calibrate thresholds from the new overload measurements. |
| Explicit placement | Met | Two stable single-replica RayServices expose workers `a` and `b`; the gateway selects one using prefix affinity plus load inputs and returns worker and reason headers. | Measure balance and affinity under shared/unique-prefix mixes. |
| Bounded visible queue | Met | Each worker has an eight-entry priority queue and four dispatch slots; interactive requests precede batch work and `orch_replica_queue_depth` exposes worker/priority depth. | Tune queue and dispatch sizes from overload results. |
| KV affinity and hop behavior | Partial | A bounded TTL affinity map records same-worker, cold-start and cold-recompute outcomes plus evictions. No KV data moves between nodes and same-worker is only a warm candidate. | Correlate affinity with engine prefix-cache hit metrics before claiming a real hit. |
| Stay/leave overflow policy | Met | Guard/admission 429 remains local; engine 503/529 and unavailability follow the declared `stay` policy and are counted by `orch_overflow_total`; the agent preserves typed status and `Retry-After`. | Add a named secondary model only if a measured capacity plan justifies leaving. |
| GPU/KV capacity reasoning | Partial | `DESIGN.md` derives 56 KiB per token and saved metrics show low KV occupancy for the successful baseline. | Record effective startup allocation and observed prompt-length distribution. |
| Application-shaped stress traffic | Partial | Both profiles have raw requests and Prometheus ranges for concurrency 1, 2 and 4. | Add repeated runs, shared/unique-prefix isolation, tenant/priority mixes and a separate overload phase. |
| Streaming per-request TTFT | Not met | Prometheus provides aggregate TTFT; `/questions` is non-streaming. | Add an SSE measurement path with request IDs and first-token timestamps. |
| Quality evaluation | Not met | Source provenance is verified, but semantic entailment and answer correctness are not scored. | Add held-out questions, expected citations and reviewer scoring. |
| Notebook and PDF | Partial | `submission.ipynb` reproduces the current A/B tables and plots from committed evidence. | Add streaming/quality results, execute the final version, and export/review the PDF. |
| Reproducible revisions | Partial | Environment files freeze Kubernetes, driver, Ray image and manifests; the model revision is recorded in `DESIGN.md`. | Pin container image digests and the model revision directly in deployment configuration. |

## Current request path

```text
user -> auth/input validation -> Graphify retrieval -> hosted SIE rerank
     -> token-aware context assembly -> nginx -> named guard -> admission
     -> stable worker placement -> bounded per-worker queue
     -> Ray Serve -> vLLM -> status-preserving response
     -> source verification -> cited response
```

A repeated live request with the same repository/scope prefix was placed on the
same stable worker with `placement=prefix_affinity` and `hop=same_worker`. That
label means the request is a warm-cache candidate; it does not claim a cache hit.

The project deliberately does not claim cross-node KV transfer. LMCache, llm-d,
Mooncake, and prefill/decode disaggregation remain outside the selected scope.
