#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/join_lambda_workers.sh --server <public-IP> \
  --worker <public-IP> --worker <public-IP> [options]

Options:
  --server-private-ip <IP>  K3s address visible to workers (auto-detected)
  --user <name>             SSH user (default: ubuntu)
  --key <path>              PEM key (default: $HOME/.ssh/week-7-key.pem)
  --storage-root <path>     Lambda filesystem mount (default: auto)

This joins exactly two fresh GPU VMs to an existing K3s server. It does not
create cloud resources and does not deploy or replace the running RayService.
EOF
}

server=""
server_private_ip=""
ssh_user="ubuntu"
key_path="${HOME}/.ssh/week-7-key.pem"
storage_root="auto"
workers=()

while (($#)); do
  case "$1" in
    --server) server="${2:?--server needs a value}"; shift 2 ;;
    --server-private-ip) server_private_ip="${2:?--server-private-ip needs a value}"; shift 2 ;;
    --worker) workers+=("${2:?--worker needs a value}"); shift 2 ;;
    --user) ssh_user="${2:?--user needs a value}"; shift 2 ;;
    --key) key_path="${2:?--key needs a value}"; shift 2 ;;
    --storage-root) storage_root="${2:?--storage-root needs a value}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$server" ]] || { echo "--server is required" >&2; exit 2; }
(( ${#workers[@]} == 2 )) || { echo "Pass exactly two --worker values" >&2; exit 2; }
[[ -f "$key_path" ]] || { echo "PEM key not found: $key_path" >&2; exit 2; }
for address in "$server" "${workers[@]}"; do
  [[ "$address" =~ ^[A-Za-z0-9._:-]+$ ]] || { echo "Invalid host: $address" >&2; exit 2; }
done
[[ "$ssh_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || { echo "Invalid SSH user" >&2; exit 2; }

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
remote_bootstrap="$project_root/scripts/bootstrap_lambda_worker_remote.sh"
ssh_options=(-i "$key_path" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
server_target="${ssh_user}@${server}"

if [[ -z "$server_private_ip" ]]; then
  server_private_ip="$(ssh "${ssh_options[@]}" "$server_target" \
    "ip -4 route get 1.1.1.1 | awk '{for (i=1;i<=NF;i++) if (\$i==\"src\") {print \$(i+1); exit}}'")"
fi
[[ "$server_private_ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || { echo "Could not determine the server private IP" >&2; exit 1; }

k3s_token="$(ssh "${ssh_options[@]}" "$server_target" 'sudo cat /var/lib/rancher/k3s/server/node-token')"
[[ -n "$k3s_token" ]] || { echo "Could not read the K3s node token" >&2; exit 1; }

echo "K3s server private address: $server_private_ip"
for worker in "${workers[@]}"; do
  target="${ssh_user}@${worker}"
  echo "Checking $target"
  ssh "${ssh_options[@]}" "$target" 'nvidia-smi --query-gpu=name,memory.total --format=csv,noheader'
  ssh "${ssh_options[@]}" "$target" 'mkdir -p "$HOME/GraphServe/scripts"'
  scp "${ssh_options[@]}" "$remote_bootstrap" "$target:GraphServe/scripts/"
  printf '%s\n%s\n' "$k3s_token" "$storage_root" \
    | ssh "${ssh_options[@]}" "$target" \
      "bash GraphServe/scripts/bootstrap_lambda_worker_remote.sh '$server_private_ip'"
done

echo "Waiting for all three Kubernetes nodes"
ssh "${ssh_options[@]}" "$server_target" \
  'sudo k3s kubectl wait --for=condition=Ready node --all --timeout=10m'
ssh "${ssh_options[@]}" "$server_target" \
  'sudo k3s kubectl label node "$(hostname -s)" graphserve.io/role=control --overwrite; sudo k3s kubectl get nodes -L graphserve.io/role,nvidia.com/gpu.present -o wide'

for worker in "${workers[@]}"; do
  node_name="$(ssh "${ssh_options[@]}" "${ssh_user}@${worker}" 'hostname -s')"
  for _ in $(seq 1 60); do
    gpu="$(ssh "${ssh_options[@]}" "$server_target" \
      "sudo k3s kubectl get node '$node_name' -o jsonpath='{.status.allocatable.nvidia\\.com/gpu}'" 2>/dev/null || true)"
    [[ "$gpu" =~ ^[1-9][0-9]*$ ]] && break
    sleep 5
  done
  [[ "${gpu:-0}" =~ ^[1-9][0-9]*$ ]] \
    || { echo "Kubernetes did not expose a GPU on $node_name" >&2; exit 1; }
done

echo "Three-node K3s cluster is ready. The existing RayService was not changed."
