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

Expected placement is Ray head, agent, gateway, nginx and SIE on the control
node, with one Ray/vLLM worker pod on each GPU node.

## Serving profiles

- `deploy/k8s/rayservice-multinode-baseline.yaml`: two replicas, automatic
  prefix caching disabled, default Ray routing.
- `deploy/k8s/rayservice-prefix-aware.yaml`: two replicas, automatic prefix
  caching enabled, `PrefixCacheAffinityRouter`.

Only two GPUs are available, so changing profiles is a deliberate stop/start
operation: a zero-downtime replacement would temporarily require four GPUs.
Always save the live manifest and environment before switching.

The measured prefix-aware profile performed acceptably at concurrency 1 and 2
but timed out four calls at concurrency 4. The baseline is therefore the
recommended active profile until the regression is isolated.
