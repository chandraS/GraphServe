#!/usr/bin/env bash
set -Eeuo pipefail

readonly K3S_VERSION="v1.34.11+k3s1"

fail() { echo "ERROR: $*" >&2; exit 1; }
trap 'echo "Worker bootstrap failed at line $LINENO" >&2' ERR

server_private_ip="${1:-}"
worker_id="${2:-}"
[[ "$server_private_ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || fail "Pass the K3s server private IPv4 address as the first argument"
[[ "$worker_id" == "a" || "$worker_id" == "b" ]] \
  || fail "Pass worker identity a or b as the second argument"
IFS= read -r k3s_token
IFS= read -r requested_storage_root
[[ -n "$k3s_token" ]] || fail "K3s token was not supplied"
[[ -n "$requested_storage_root" ]] || requested_storage_root="auto"

command -v nvidia-smi >/dev/null || fail "nvidia-smi is unavailable"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader

if systemctl list-unit-files k3s.service >/dev/null 2>&1; then
  fail "This VM already runs a K3s server; use a fresh worker VM"
fi

if [[ "$requested_storage_root" == "auto" ]]; then
  shopt -s nullglob
  lambda_filesystems=(/lambda/nfs/*)
  shopt -u nullglob
  (( ${#lambda_filesystems[@]} == 1 )) \
    || fail "Expected exactly one filesystem below /lambda/nfs; pass its mount path explicitly"
  filesystem_root="${lambda_filesystems[0]}"
else
  filesystem_root="${requested_storage_root%/}"
  [[ "$filesystem_root" == /lambda/nfs/* && -d "$filesystem_root" ]] \
    || fail "Storage root must be a mounted directory below /lambda/nfs"
fi

storage_root="$filesystem_root/graphserve"
sudo mkdir -p "$storage_root"/{huggingface,sie,repositories,benchmarks}
sudo chmod 0777 "$storage_root"/{huggingface,sie,repositories,benchmarks}
if [[ -e /mnt/graphserve-data && ! -L /mnt/graphserve-data ]]; then
  fail "/mnt/graphserve-data exists and is not a symlink"
fi
sudo ln -sfn "$storage_root" /mnt/graphserve-data

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg2

if ! command -v nvidia-container-runtime >/dev/null; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update
  toolkit_version="1.20.1-1"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    "nvidia-container-toolkit=${toolkit_version}" \
    "nvidia-container-toolkit-base=${toolkit_version}" \
    "libnvidia-container-tools=${toolkit_version}" \
    "libnvidia-container1=${toolkit_version}"
fi

curl --fail --insecure --silent --show-error --connect-timeout 5 \
  "https://${server_private_ip}:6443/ping" >/dev/null \
  || fail "Cannot reach the K3s API at ${server_private_ip}:6443 over the private network"

sudo mkdir -p /etc/rancher/k3s
sudo install -m 0600 /dev/null /etc/rancher/k3s/config.yaml
sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<EOF
server: "https://${server_private_ip}:6443"
token: "${k3s_token}"
default-runtime: nvidia
node-label:
  - "graphserve.io/role=gpu-worker"
  - "graphserve.io/worker-id=${worker_id}"
  - "nvidia.com/gpu.present=true"
EOF

if ! command -v k3s >/dev/null; then
  curl -fsSL https://get.k3s.io -o /tmp/install-k3s.sh
  sudo env INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_EXEC=agent \
    sh /tmp/install-k3s.sh
else
  sudo systemctl restart k3s-agent
fi

sudo systemctl is-active --quiet k3s-agent || fail "K3s agent is not active"
sudo grep -q nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml \
  || fail "K3s did not detect the NVIDIA runtime"

echo "Worker joined: $(hostname -s)"
echo "GraphServe storage: $storage_root"
