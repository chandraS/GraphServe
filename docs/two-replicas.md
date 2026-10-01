# Historical single-A100 two-replica experiment

This profile predates the measured three-node deployment. It attempted to fit
two independent Ray Serve/vLLM processes on one A100-SXM4-40GB by assigning each
replica half of Ray's logical GPU resource and 42% of physical GPU memory.

The experiment used:

- `deploy/k8s/rayservice-two-replicas.yaml`;
- two BF16 model copies on one physical GPU;
- `max_model_len=8192`;
- `max_num_seqs=2` and `max_ongoing_requests=2` per replica;
- eager execution to reduce CUDA graph memory.

This is not the current deployment and is not evidence for two independent
workers or fault domains. The final measured topology uses two separate A100
worker nodes, one whole-GPU replica per node, as described in
`three-node-prefix-routing.md`.

The historical helper remains available for constrained local experiments:

```bash
./scripts/set_llm_replicas.sh \
  --host CONTROL_PUBLIC_IP \
  --replicas 2 \
  --replace-running-service
```

Do not use this profile for the committed A/B comparison. Use
`rayservice-multinode-baseline.yaml` and `rayservice-prefix-aware.yaml` on the
three-node cluster instead.
