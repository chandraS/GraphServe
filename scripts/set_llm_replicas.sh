#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/set_llm_replicas.sh --host <IP> --replicas <1|2> --replace-running-service [options]

Recreates the RayService with either the baseline one-replica manifest or the
single-A100 experimental two-replica manifest. Expect several minutes of model
service downtime. The repository/model caches remain on the attached filesystem.

Options:
  --user <name>       SSH user (default: ubuntu)
  --key <path>        PEM key (default: $HOME/.ssh/week-7-key.pem)
  --remote-dir <path> Remote checkout (default: /home/<user>/GraphServe)
  --help              Show this message
USAGE
}

host=""
ssh_user="ubuntu"
key_path="${HOME}/.ssh/week-7-key.pem"
remote_dir=""
replicas=""
replace="false"
while (($#)); do
  case "$1" in
    --host) host="${2:?--host needs a value}"; shift 2 ;;
    --user) ssh_user="${2:?--user needs a value}"; shift 2 ;;
    --key) key_path="${2:?--key needs a value}"; shift 2 ;;
    --remote-dir) remote_dir="${2:?--remote-dir needs a value}"; shift 2 ;;
    --replicas) replicas="${2:?--replicas needs a value}"; shift 2 ;;
    --replace-running-service) replace="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$host" && "$host" =~ ^[A-Za-z0-9._:-]+$ ]] || { echo "A valid --host is required" >&2; exit 2; }
[[ "$replicas" == "1" || "$replicas" == "2" ]] || { echo "--replicas must be 1 or 2" >&2; exit 2; }
[[ "$replace" == "true" ]] || { echo "Pass --replace-running-service to acknowledge temporary inference downtime" >&2; exit 2; }
[[ "$ssh_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || { echo "Invalid SSH user" >&2; exit 2; }
[[ -f "$key_path" ]] || { echo "PEM key not found: $key_path" >&2; exit 2; }
remote_dir="${remote_dir:-/home/${ssh_user}/GraphServe}"
[[ "$remote_dir" =~ ^/[A-Za-z0-9._/-]+$ && "$remote_dir" != *".."* ]] || { echo "Invalid remote directory" >&2; exit 2; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${ssh_user}@${host}"
ssh_opts=(-i "$key_path" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)
rsync -az -e "ssh -i '$key_path' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  "$root/deploy/k8s/rayservice.yaml" "$root/deploy/k8s/rayservice-two-replicas.yaml" \
  "$target:$remote_dir/deploy/k8s/"

if [[ "$replicas" == "2" ]]; then
  manifest="deploy/k8s/rayservice-two-replicas.yaml"
else
  manifest="deploy/k8s/rayservice.yaml"
fi

echo "Replacing RayService with $replicas model replica(s); inference will be temporarily unavailable"
ssh "${ssh_opts[@]}" "$target" \
  "cd '$remote_dir' && sudo k3s kubectl -n repo-agent delete rayservice repo-llm --ignore-not-found --wait=true && sudo k3s kubectl -n repo-agent apply -f '$manifest'"

ready="false"
for _ in $(seq 1 120); do
  serve_status="$(ssh "${ssh_opts[@]}" "$target" '
    head=$(sudo k3s kubectl -n repo-agent get pod -l ray.io/node-type=head -o jsonpath="{.items[0].metadata.name}" 2>/dev/null || true)
    if [[ -n "$head" ]]; then
      sudo k3s kubectl -n repo-agent exec "$head" -c ray-head -- serve status --address http://127.0.0.1:8265 2>/dev/null || true
    fi' 2>/dev/null || true)"
  model_block="$(printf '%s\n' "$serve_status" | sed -n '/LLMServer:qwen-coder:/,/OpenAiIngress:/p')"
  if grep -q 'status: HEALTHY' <<<"$model_block" && grep -q "RUNNING: $replicas" <<<"$model_block"; then
    ready="true"
    printf '%s\n' "$serve_status"
    break
  fi
  sleep 15
done

if [[ "$ready" != "true" ]]; then
  echo "Ray Serve did not reach $replicas healthy model replica(s) within 30 minutes" >&2
  if [[ "$replicas" == "2" ]]; then
    echo "Rolling back to the one-replica baseline" >&2
    ssh "${ssh_opts[@]}" "$target" \
      "cd '$remote_dir' && sudo k3s kubectl -n repo-agent delete rayservice repo-llm --ignore-not-found --wait=true && sudo k3s kubectl -n repo-agent apply -f deploy/k8s/rayservice.yaml"
  fi
  exit 1
fi

ssh "${ssh_opts[@]}" "$target" '
  sudo k3s kubectl -n repo-agent get pods -o wide
  worker=$(sudo k3s kubectl -n repo-agent get pod -l ray.io/node-type=worker -o jsonpath="{.items[0].metadata.name}")
  sudo k3s kubectl -n repo-agent exec "$worker" -c ray-worker -- nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader
'
