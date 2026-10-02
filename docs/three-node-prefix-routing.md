# Three-node serving deployment

The measured topology has one K3s control node and two A100 worker nodes. Each
worker runs one complete Qwen/vLLM replica with its own GPU-resident KV cache.
No KV data moves between nodes.

All instances must be in the same private network, use the same SSH key, and
mount the same Lambda filesystem at `/mnt/graphserve-data`. The shared filesystem
persists Hugging Face weights, SIE cache, repository snapshots and benchmarks;
K3s node identity and certificates are rebuilt for each new cluster.

## Build or rejoin the cluster

Bootstrap the control node with `scripts/deploy_lambda.sh`, then join the two
workers without running the full single-node deploy on them:

```bash
./scripts/join_lambda_workers.sh \
  --server CONTROL_PUBLIC_IP \
  --worker WORKER_1_PUBLIC_IP \
  --worker WORKER_2_PUBLIC_IP
```

When address discovery selects the wrong interface, add
`--server-private-ip CONTROL_PRIVATE_IP`. The script is idempotent, installs the
NVIDIA runtime without replacing K3s containerd, labels node roles, and waits for
one allocatable GPU on each worker.

```bash
sudo k3s kubectl get nodes -L graphserve.io/role,nvidia.com/gpu.present -o wide
sudo k3s kubectl get pods -n repo-agent -o wide
```

Expected placement is the agent, gateway, nginx and two Ray heads on the
control node, with one single-replica Ray/vLLM service on each labeled GPU node.
Hosted SIE runs outside the cluster.

## Serving profiles

Deploy either final profile through `scripts/deploy_orchestrated.sh`:

- `baseline`: two stable single-replica RayServices with automatic prefix
  caching disabled.
- `prefix-cache`: the same topology and gateway policy with automatic prefix
  caching enabled.

The gateway chooses worker `a` or `b`, enqueues the request once, and exposes the
worker, placement reason, queue wait and hop outcome as response headers and
Prometheus metrics. Same-worker affinity is a warm candidate; changing workers
is recorded as cold recomputation. No KV tensors move between nodes.

The older shared-RayService manifests and measurements remain as historical
baseline evidence. Final results must be remeasured through the explicit
orchestration path.
