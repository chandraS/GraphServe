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
| Explicit guard before GPU work | Partial | Authentication, body limits, model/output validation, URL/SHA validation, path containment and source verification exist. | Add a named guard decision, reason-labelled metrics and direct gateway tests. |
| Admission control | Not met | The gateway has a four-slot process-local semaphore and a one-second acquisition timeout. | Add tenant/token budgets, deadline and queue estimates, KV/health inputs, typed rejection reasons and `Retry-After`. |
| Explicit placement | Partial | Ray's default router or prefix-affinity router selects between two replicas. The gateway cannot select or identify a specific worker. | Expose stable worker endpoints or worker-aware routing and record the selected worker and route reason. |
| Bounded visible queue | Not met | The semaphore bounds in-flight calls but exposes no per-worker queue, priority or depth metric. | Add bounded per-worker queues and `orch_replica_queue_depth`. |
| KV affinity and hop behavior | Partial | Configuration B enables per-replica automatic prefix caching and prefix-affinity routing. No KV data moves between nodes. | Record prefix hits, cold recomputation and stale affinity eviction. A real KV-transfer requirement would need a separate backend. |
| Stay/leave overflow policy | Not met | Busy capacity returns 429; upstream failures become 503 at the gateway and usually 502 at the agent. No overflow provider is configured. | Preserve typed status codes and add an explicit, bounded overflow policy only if the rubric requires a real provider. |
| GPU/KV capacity reasoning | Partial | `DESIGN.md` derives 56 KiB per token and saved metrics show low KV occupancy for the successful baseline. | Record effective startup allocation and observed prompt-length distribution. |
| Application-shaped stress traffic | Partial | Both profiles have raw requests and Prometheus ranges for concurrency 1, 2 and 4. | Add repeated runs, shared/unique-prefix isolation, tenant/priority mixes and a separate overload phase. |
| Streaming per-request TTFT | Not met | Prometheus provides aggregate TTFT; `/questions` is non-streaming. | Add an SSE measurement path with request IDs and first-token timestamps. |
| Quality evaluation | Not met | Source provenance is verified, but semantic entailment and answer correctness are not scored. | Add held-out questions, expected citations and reviewer scoring. |
| Notebook and PDF | Partial | `submission.ipynb` reproduces the current A/B tables and plots from committed evidence. | Add streaming/quality results, execute the final version, and export/review the PDF. |
| Reproducible revisions | Partial | Environment files freeze Kubernetes, driver, Ray image and manifests; the model revision is recorded in `DESIGN.md`. | Pin container image digests and the model revision directly in deployment configuration. |

## Measured request path

```text
user -> auth/input validation -> Graphify retrieval -> SIE rerank
     -> token-aware context assembly -> nginx -> gateway semaphore
     -> Ray Serve router -> one of two vLLM replicas
     -> source verification -> cited response
```

## Remaining target path

```text
user -> retrieval -> named guard -> admission -> worker placement
     -> bounded worker queue -> same-worker cache affinity or cold recompute
     -> vLLM -> status-preserving response -> source verification
```

The project deliberately does not claim cross-node KV transfer. LMCache, llm-d,
Mooncake, and prefill/decode disaggregation remain outside the selected scope.
