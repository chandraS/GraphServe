#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/deploy_lambda.sh --host <IP-or-hostname> [options]

Options:
  --user <name>       SSH user (default: ubuntu)
  --key <path>        PEM key (default: $HOME/.ssh/week-7-key.pem)
  --remote-dir <path> Remote project directory (default: /home/<user>/GraphServe)
  --api-key <value>   Existing GraphServe API key; generated when omitted
  --storage-root <path>
                      Attached Lambda filesystem path (default: auto-detect /lambda/nfs/*)
  --skip-monitoring   Skip Prometheus and Grafana installation
  --help              Show this message

The script syncs this checkout, bootstraps a single-node GPU K3s cluster,
builds the GraphServe image on the VM, and deploys the application.
EOF
}

host=""
ssh_user="ubuntu"
key_path="${HOME}/.ssh/week-7-key.pem"
remote_dir=""
api_key="${AGENT_API_KEY:-}"
skip_monitoring="false"
storage_root="auto"

while (($#)); do
  case "$1" in
    --host) host="${2:?--host needs a value}"; shift 2 ;;
    --user) ssh_user="${2:?--user needs a value}"; shift 2 ;;
    --key) key_path="${2:?--key needs a value}"; shift 2 ;;
    --remote-dir) remote_dir="${2:?--remote-dir needs a value}"; shift 2 ;;
    --api-key) api_key="${2:?--api-key needs a value}"; shift 2 ;;
    --storage-root) storage_root="${2:?--storage-root needs a value}"; shift 2 ;;
    --skip-monitoring) skip_monitoring="true"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$host" ]] || { echo "--host is required" >&2; usage >&2; exit 2; }
[[ "$host" =~ ^[A-Za-z0-9._:-]+$ ]] || { echo "Invalid host" >&2; exit 2; }
[[ "$ssh_user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || { echo "Invalid SSH user" >&2; exit 2; }
[[ -f "$key_path" ]] || { echo "PEM key not found: $key_path" >&2; exit 2; }

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
remote_dir="${remote_dir:-/home/${ssh_user}/GraphServe}"
[[ "$remote_dir" =~ ^/[A-Za-z0-9._/-]+$ && "$remote_dir" != *".."* ]] || { echo "Remote directory must be an absolute path using letters, numbers, dots, underscores, dashes and slashes" >&2; exit 2; }
[[ "$storage_root" == "auto" || ( "$storage_root" =~ ^/lambda/nfs/[A-Za-z0-9._/-]+$ && "$storage_root" != *".."* ) ]] || { echo "Storage root must be auto or an absolute path below /lambda/nfs" >&2; exit 2; }

for command_name in ssh rsync openssl; do
  command -v "$command_name" >/dev/null || { echo "Missing local command: $command_name" >&2; exit 2; }
done

chmod 600 "$key_path"
ssh_options=(-i "$key_path" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10)
target="${ssh_user}@${host}"

credential_file="$project_root/artifacts/lambda-deployment.env"
if [[ -z "$api_key" && -f "$credential_file" ]]; then
  api_key="$(sed -n 's/^AGENT_API_KEY=//p' "$credential_file" | head -1)"
fi
if [[ -z "$api_key" ]]; then
  api_key="$(openssl rand -hex 24)"
fi
umask 077
printf 'AGENT_API_KEY=%s\n' "$api_key" > "$credential_file"

echo "Checking SSH connectivity to $target"
ssh "${ssh_options[@]}" "$target" 'uname -s'
if ! ssh "${ssh_options[@]}" "$target" 'nvidia-smi --query-gpu=name,memory.total --format=csv,noheader'; then
  echo "NVIDIA GPU detected without a working driver; installing Ubuntu's data-center driver"
  ssh "${ssh_options[@]}" "$target" 'grep -q 0x10de /sys/bus/pci/devices/*/vendor
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "linux-headers-$(uname -r)" nvidia-driver-570-server-open'
  echo "Rebooting the VM once to load the NVIDIA kernel modules"
  ssh "${ssh_options[@]}" "$target" 'sudo systemctl reboot' || true
  gpu_ready=false
  for _ in $(seq 1 36); do
    if ssh "${ssh_options[@]}" "$target" 'nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader' 2>/dev/null; then
      gpu_ready=true
      break
    fi
    sleep 5
  done
  [[ "$gpu_ready" == "true" ]] || { echo "VM did not return with a working NVIDIA driver" >&2; exit 1; }
fi

echo "Syncing GraphServe to $target:$remote_dir"
ssh "${ssh_options[@]}" "$target" "mkdir -p '$remote_dir'"
rsync -az \
  --exclude .git \
  --exclude .venv \
  --exclude .env \
  --exclude .pytest_cache \
  --exclude __pycache__ \
  --exclude 'artifacts/*' \
  -e "ssh -i '$key_path' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  "$project_root/" "$target:$remote_dir/"

echo "Bootstrapping and deploying on the VM; image and model downloads can take 20-60 minutes"
printf '%s\n%s\n%s\n' "$api_key" "$skip_monitoring" "$storage_root" | \
  ssh "${ssh_options[@]}" "$target" "cd '$remote_dir' && bash scripts/bootstrap_lambda_remote.sh"

cat <<EOF

Deployment command completed.
API key saved locally at: $credential_file

Open the app through an SSH tunnel:
  ssh -i '$key_path' -L 8080:127.0.0.1:8080 '$target'

Then visit:
  http://localhost:8080
EOF

if [[ "$skip_monitoring" == "false" ]]; then
  cat <<EOF

Grafana tunnel (run in another terminal):
  ssh -i '$key_path' -L 3000:127.0.0.1:3000 '$target'
Then visit http://localhost:3000

Prometheus tunnel (used by scripts/stress_test.py):
  ssh -i '$key_path' -L 9090:127.0.0.1:9090 '$target'
EOF
fi
