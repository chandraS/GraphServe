# Lambda K3s deployment

GraphServe uses one control node and two A100 worker nodes. The repository does
not create, resize or terminate paid resources.

## Persistent storage

Attach the same Lambda filesystem to all three instances before launch. The
deployment stores Hugging Face weights, SIE cache, repository snapshots and
benchmark artifacts below its `graphserve/` directory. All nodes must expose
that filesystem at `/mnt/graphserve-data` for the multi-node manifests.

Do not preserve or restore the K3s data directory across replacement VMs. Node
certificates and identity are rebuilt; persistent application data is reattached.

## Bootstrap the control node

```bash
./scripts/deploy_lambda.sh --host CONTROL_PUBLIC_IP
```

The script reuses compatible existing Docker/containerd packages, installs K3s,
the NVIDIA runtime/device plugin, KubeRay and monitoring, builds the application
image, deploys GraphServe and SIE, and saves the generated agent key in the
ignored local file `artifacts/lambda-deployment.env`.

## Join two GPU workers

```bash
./scripts/join_lambda_workers.sh \
  --server CONTROL_PUBLIC_IP \
  --worker WORKER_1_PUBLIC_IP \
  --worker WORKER_2_PUBLIC_IP
```

The join script installs the K3s agent and NVIDIA runtime integration without
replacing K3s containerd. It labels the control and worker roles and waits for
Kubernetes to advertise one GPU on each worker.

## Deploy a measured profile

```bash
kubectl -n repo-agent apply -f deploy/k8s/rayservice-multinode-baseline.yaml
kubectl -n repo-agent wait --for=condition=Ready rayservice/repo-llm --timeout=60m
```

The prefix-aware alternative is
`deploy/k8s/rayservice-prefix-aware.yaml`. With only two GPUs, stop the current
RayService before switching profiles because old and new clusters cannot coexist
during KubeRay's replacement rollout.

Validate placement and health:

```bash
kubectl get nodes -L graphserve.io/role,nvidia.com/gpu.present -o wide
kubectl -n repo-agent get pods -o wide
kubectl -n repo-agent get rayservice repo-llm
```

## Private access

nginx, Grafana and Prometheus listen on control-node loopback. Tunnel them:

```bash
ssh -i "$HOME/.ssh/week-7-key.pem" -N \
  -L 8080:127.0.0.1:8080 \
  -L 3000:127.0.0.1:3000 \
  -L 9090:127.0.0.1:9090 \
  ubuntu@CONTROL_PUBLIC_IP
```

Use `http://localhost:8080`, `http://localhost:3000`, and
`http://localhost:9090`. Keep Ray and vLLM private.

## Rebuild and cost behavior

Lambda instances cannot be paused without continuing cost. Terminate instances
when measurement ends; the shared filesystem preserves expensive model and SIE
downloads. A later cluster is recreated with the same scripts and reuses those
caches. Confirm benchmark results have also been copied into the repository
before terminating the final nodes.
