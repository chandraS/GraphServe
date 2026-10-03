#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/deploy_orchestrated.sh --host <control-public-IP> \
  --profile <baseline|prefix-cache> --replace-running-service [options]

Builds the GraphServe agent/gateway image, labels the two GPU workers a/b,
replaces the shared RayService with one stable RayService per worker, and enables
explicit gateway guard/admit/place/queue control.

Options:
  --user <name>       SSH user (default: ubuntu)
  --key <path>        PEM key (default: $HOME/.ssh/week-7-key.pem)
  --remote-dir <path> Remote checkout (default: /home/<user>/GraphServe)
EOF
}

host=""
profile=""
replace="false"
ssh_user="ubuntu"
key_path="${HOME}/.ssh/week-7-key.pem"
remote_dir=""
while (($#)); do
  case "$1" in
    --host) host="${2:?--host needs a value}"; shift 2 ;;
    --profile) profile="${2:?--profile needs a value}"; shift 2 ;;
    --replace-running-service) replace="true"; shift ;;
    --user) ssh_user="${2:?--user needs a value}"; shift 2 ;;
    --key) key_path="${2:?--key needs a value}"; shift 2 ;;
    --remote-dir) remote_dir="${2:?--remote-dir needs a value}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$host" && "$host" =~ ^[A-Za-z0-9._:-]+$ ]] || { echo "A valid --host is required" >&2; exit 2; }
[[ "$profile" == "baseline" || "$profile" == "prefix-cache" ]] || { echo "--profile must be baseline or prefix-cache" >&2; exit 2; }
[[ "$replace" == "true" ]] || { echo "Pass --replace-running-service to acknowledge inference downtime" >&2; exit 2; }
[[ -f "$key_path" ]] || { echo "PEM key not found: $key_path" >&2; exit 2; }
[[ "$ssh_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || { echo "Invalid SSH user" >&2; exit 2; }
remote_dir="${remote_dir:-/home/${ssh_user}/GraphServe}"
[[ "$remote_dir" =~ ^/[A-Za-z0-9._/-]+$ && "$remote_dir" != *".."* ]] || { echo "Invalid remote directory" >&2; exit 2; }

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${ssh_user}@${host}"
ssh_opts=(-i "$key_path" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new)
manifest="deploy/k8s/rayservices-orchestrated-${profile}.yaml"

echo "Syncing GraphServe to $target:$remote_dir"
rsync -az \
  --exclude .git --exclude .venv --exclude .env --exclude .pytest_cache \
  --exclude __pycache__ --exclude 'artifacts/*' \
  -e "ssh -i '$key_path' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  "$root/" "$target:$remote_dir/"

ssh "${ssh_opts[@]}" "$target" "bash -s -- '$remote_dir' '$manifest'" <<'REMOTE'
set -Eeuo pipefail
remote_dir="$1"
manifest="$2"
cd "$remote_dir"
kctl() { sudo k3s kubectl "$@"; }

mapfile -t workers < <(kctl get nodes -l graphserve.io/role=gpu-worker -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)
[[ "${#workers[@]}" == "2" ]] || { echo "Expected exactly two gpu-worker nodes, found ${#workers[@]}" >&2; exit 1; }
kctl label node "${workers[0]}" graphserve.io/worker-id=a --overwrite
kctl label node "${workers[1]}" graphserve.io/worker-id=b --overwrite
kctl get nodes -L graphserve.io/role,graphserve.io/worker-id,nvidia.com/gpu.present

image_archive=/tmp/repo-agent-dev.oci.tar
sudo rm -f "$image_archive"
sudo docker buildx build --pull --output "type=oci,dest=$image_archive" -t repo-agent:dev .
sudo k3s ctr images import "$image_archive"
sudo rm -f "$image_archive"

kctl apply --dry-run=server -f "$manifest" >/dev/null
kctl -n repo-agent delete rayservice repo-llm repo-llm-a repo-llm-b --ignore-not-found --wait=true
kctl -n repo-agent apply -f "$manifest"
kctl -n repo-agent wait --for=condition=Ready rayservice/repo-llm-a --timeout=60m
kctl -n repo-agent wait --for=condition=Ready rayservice/repo-llm-b --timeout=60m

kctl apply -f deploy/k8s/orchestrator-config.yaml
kctl -n repo-agent create configmap nginx-config \
  --from-file=nginx.conf=deploy/k8s/nginx.conf \
  --dry-run=client -o yaml | kctl apply -f -
kctl -n repo-agent apply -f deploy/k8s/application.yaml
kctl apply -f deploy/k8s/grafana-dashboard.yaml
kctl -n repo-agent rollout restart deployment/agent deployment/gateway deployment/nginx
kctl -n repo-agent rollout status deployment/agent --timeout=10m
kctl -n repo-agent rollout status deployment/gateway --timeout=10m
kctl -n repo-agent rollout status deployment/nginx --timeout=10m

# A kubectl service port-forward is bound to the selected pod and exits when an
# nginx rollout replaces that pod. Recreate it so the outer SSH tunnel continues
# to have a control-node loopback endpoint.
if ! pgrep -f 'kubectl.*port-forward.*svc/nginx.*8080:8080' >/dev/null; then
  nohup sudo k3s kubectl -n repo-agent \
    port-forward --address 127.0.0.1 svc/nginx 8080:8080 \
    >/tmp/graphserve-port-forward.log 2>&1 </dev/null &
fi
for _ in $(seq 1 20); do
  curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null && break
  sleep 1
done
curl --fail http://127.0.0.1:8080/healthz

kctl -n repo-agent get rayservice,pods -o wide
REMOTE

echo "Orchestrated profile $profile is ready."
