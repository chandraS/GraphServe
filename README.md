# Repository understanding and change planning

A Python agent accepts a public GitHub repository URL and a **full 40-character commit SHA**, extracts a Graphify graph, retrieves related source, and returns cited explanations and change proposals. The scaffold never applies proposed changes or runs repository programs.

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
cp .env.example .env
docker compose up --build
```

Open http://localhost:8080/docs for the API, http://localhost:3000 for Grafana (local default admin/admin), and http://localhost:9090 for Prometheus. Compose defaults to **mock inference**, uses real Git/Graphify ingestion, and does not download Qwen weights. Graphify must be on PATH when running outside Docker. Alternatively:

```bash
source .venv/bin/activate
INFERENCE_MODE=mock AGENT_API_KEY=local-development-only uvicorn repo_agent.api:app --port 8080
```

In Swagger (`/docs`), click **Authorize**, enter only `local-development-only` (without the `Bearer` prefix), and click **Authorize**, then **Close**. Use your configured key if you changed it. Swagger adds the Authorization header automatically.

Submit `POST /repositories` with `{"url":"https://github.com/OWNER/REPO","commit":"FULL_40_CHARACTER_SHA"}` and header `Authorization: Bearer local-development-only`. Use the returned `repository_id` in `POST /questions`: `{"repository_id":"...","question":"Explain the request path and propose adding caching"}`. Responses contain structured claims, proposals, limitations, and pinned GitHub line URLs. Mock answers are explicitly labeled and are not measured model results.

```bash
pytest -q
kubectl kustomize deploy/k8s > /tmp/repo-agent-rendered.yaml
```

## Architecture

```mermaid
flowchart LR
    U[User] --> N[nginx :8080]
    N --> A[Python agent]
    A --> G[Graphify relationships]
    A --> B[Commit-pinned Git blobs]
    A -. optional embeddings .-> S[SIE]
    A --> I[nginx :8081 internal]
    I --> W[Inference gateway]
    W --> R[Ray Serve LLM]
    R --> V[vLLM / Qwen Coder 7B BF16]
    A --> P[Prometheus]
    W --> P
    R --> P
    P --> F[Grafana]
```

The agent owns tool execution, lexical seed search, one-hop dependency/impact traversal, source verification, and Qwen chat-template token counting. Context reserves 1,024 output tokens plus 128 safety tokens within an 8,192-token window. Only Git and Graphify subprocesses are used, without shell execution. Repository text is treated as untrusted model input. Unsupported/unknown citations fail closed. Source checks establish provenance and exact excerpt contents, **not semantic entailment**.

SIE is selected for embeddings, not the archived Superlinked vector framework and not Qwen generation. Set `SIE_BASE_URL`, `SIE_MODEL`, and optionally `SIE_API_KEY` to an existing SIE deployment. The adapter calls `/v1/embeddings` and ranks Graphify candidate excerpts by cosine similarity in Python. This initial adapter reranks candidates; it is not yet a persistent full-repository vector index. It has no silent fallback on SIE failure. See [SIE documentation](https://github.com/superlinked/sie). A separate optional CPU profile is provided in `compose.sie.yaml`; pin its image digest before reproducible evaluation.

## Deployment and status

See [K3s deployment plan](docs/deployment.md), [assignment milestones](docs/assignment.md), and [validation record](docs/validation.md). No cloud resources were provisioned. The Kubernetes resources are a scaffold awaiting a real GPU environment, image publication/import, authentication secret, CRDs, and capacity validation.

Current limits: public GitHub repositories only; synchronous bounded-time ingestion; one API process and one data volume; lexical/graph retrieval can miss relevant files; code-only Graphify skips semantic document extraction; no arbitrary execution, private-repo credentials, edit application, or multi-tenant isolation. For untrusted/public hosted ingestion, add isolated ingestion Jobs, repository disk quotas, request/job queues and per-user authorization before opening network access.
