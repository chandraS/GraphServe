# Validation record

Workspace inspection (2026-09-27): directory was empty and was not a Git repository. Python 3.12.3, Git, Docker 29.6.2, kubectl client 1.30.0 and Helm 3.15.1 were available. Graphify and uv were initially absent. Created a local virtual environment, installed Graphify 0.9.69 and application dependencies, and recorded exact resolved versions in `requirements.lock`. No existing source files were overwritten. No cluster context was contacted.

Completed checks:
- Six pytest checks: real Graphify extraction against a local committed fixture; immutable Git-blob reads despite worktree changes; traversal/symlink rejection; URL/full-SHA validation; unknown citation rejection; context overflow; API authentication and mock Q&A; complete ingestion with local transport substituted for GitHub; SIE embedding response ordering with mocked HTTP.
- Docker Compose configuration validated, including the optional SIE override. Agent and gateway container images built successfully with the locked Python dependencies (local Linux ARM64 Docker engine).
- Kustomize rendered 13 objects, and the embedded Ray Serve configuration matched `deploy/serve.yaml`.
- Python source compilation passed.

The test runner reports a Starlette/httpx deprecation warning; tests pass. This is not a Kubernetes schema/server-side validation or GPU test. Public GitHub network ingestion, real Qwen tokenizer/model startup, live SIE inference, Ray/vLLM GPU execution, cluster readiness and scraped serving-series names still require environment validation. No model benchmarks or cloud deployment were performed.
