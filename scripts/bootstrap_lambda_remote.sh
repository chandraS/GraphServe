#!/usr/bin/env bash
set -Eeuo pipefail

readonly K3S_VERSION="v1.34.11+k3s1"
readonly NVIDIA_PLUGIN_VERSION="0.19.3"
readonly KUBERAY_VERSION="1.7.0"
readonly HELM_VERSION="v3.22.0"
readonly MONITORING_CHART_VERSION="91.4.1"

fail() { echo "ERROR: $*" >&2; exit 1; }
trap 'echo "Deployment failed at line $LINENO" >&2' ERR

IFS= read -r agent_api_key
IFS= read -r skip_monitoring
IFS= read -r requested_storage_root
[[ -n "$agent_api_key" ]] || fail "API key was not supplied by the local deploy script"
[[ "$skip_monitoring" == "true" || "$skip_monitoring" == "false" ]] || fail "Invalid monitoring setting"
[[ -n "$requested_storage_root" ]] || requested_storage_root="auto"
[[ "$(uname -s)" == "Linux" ]] || fail "This script must run on the Lambda Linux VM"
command -v nvidia-smi >/dev/null || fail "nvidia-smi is unavailable; select a Lambda GPU image"

echo "== VM preflight =="
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
free -h
df -h /
gpu_count="$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ')"
[[ "$gpu_count" -ge 1 ]] || fail "No NVIDIA GPU detected"

echo "== Persistent application storage =="
if [[ "$requested_storage_root" == "auto" ]]; then
  shopt -s nullglob
  lambda_filesystems=(/lambda/nfs/*)
  shopt -u nullglob
  if (( ${#lambda_filesystems[@]} == 1 )); then
    storage_root="${lambda_filesystems[0]}/graphserve"
  elif (( ${#lambda_filesystems[@]} == 0 )); then
    storage_root="/var/lib/graphserve-local"
    echo "WARNING: no Lambda filesystem found; application data will be lost when this VM is terminated"
  else
    fail "Multiple Lambda filesystems found. Re-run with --storage-root /lambda/nfs/<name>"
  fi
else
  storage_root="${requested_storage_root%/}/graphserve"
  [[ -d "${requested_storage_root%/}" ]] || fail "Storage root is not mounted: ${requested_storage_root%/}"
fi
sudo mkdir -p "$storage_root"/{huggingface,sie,repositories,benchmarks}
sudo chmod 0777 "$storage_root"/{huggingface,sie,repositories,benchmarks}
if [[ -e /mnt/graphserve-data && ! -L /mnt/graphserve-data ]]; then
  fail "/mnt/graphserve-data exists and is not the managed symlink"
fi
sudo ln -sfn "$storage_root" /mnt/graphserve-data
echo "GraphServe data root: $storage_root"

echo "== Base packages =="
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg2
if command -v docker >/dev/null; then
  echo "Using existing Docker installation: $(docker --version)"
else
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker.io
fi
sudo systemctl enable --now docker

if ! command -v nvidia-container-runtime >/dev/null; then
  echo "== NVIDIA Container Toolkit =="
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
command -v nvidia-container-runtime >/dev/null || fail "NVIDIA container runtime installation failed"

echo "== K3s =="
sudo mkdir -p /etc/rancher/k3s
printf '%s\n' \
  'write-kubeconfig-mode: "0600"' \
  'default-runtime: nvidia' \
  'disable:' \
  '  - traefik' \
  '  - servicelb' \
  | sudo tee /etc/rancher/k3s/config.yaml >/dev/null

if ! command -v k3s >/dev/null; then
  curl -fsSL https://get.k3s.io -o /tmp/install-k3s.sh
  sudo env INSTALL_K3S_VERSION="$K3S_VERSION" sh /tmp/install-k3s.sh
else
  echo "K3s already installed: $(k3s --version | head -1)"
  sudo systemctl restart k3s
fi
sudo systemctl is-active --quiet k3s || fail "K3s service is not active"
mkdir -p "$HOME/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config"
sudo chown "$(id -u):$(id -g)" "$HOME/.kube/config"
chmod 600 "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"
kctl() { k3s kubectl "$@"; }
kctl wait --for=condition=Ready node --all --timeout=5m
sudo grep -q nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml \
  || fail "K3s did not detect the NVIDIA runtime"

echo "== Helm =="
if ! command -v helm >/dev/null || [[ "$(helm version --template '{{.Version}}')" != "$HELM_VERSION" ]]; then
  machine_arch="$(uname -m)"
  case "$machine_arch" in
    x86_64) helm_arch="amd64" ;;
    aarch64|arm64) helm_arch="arm64" ;;
    *) fail "Unsupported architecture for Helm: $machine_arch" ;;
  esac
  archive="helm-${HELM_VERSION}-linux-${helm_arch}.tar.gz"
  helm_tmp="$(mktemp -d)"
  curl -fsSLo "${helm_tmp}/${archive}" "https://get.helm.sh/${archive}"
  curl -fsSLo "${helm_tmp}/${archive}.sha256sum" "https://get.helm.sh/${archive}.sha256sum"
  expected_checksum="$(awk '{print $1}' "${helm_tmp}/${archive}.sha256sum")"
  printf '%s  %s\n' "$expected_checksum" "${helm_tmp}/${archive}" | sha256sum -c -
  tar -xzf "${helm_tmp}/${archive}" -C "$helm_tmp"
  sudo install -m 0755 "${helm_tmp}/linux-${helm_arch}/helm" /usr/local/bin/helm
  rm -rf "$helm_tmp"
fi
helm version --short

echo "== NVIDIA device plugin =="
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin --force-update
helm repo add kuberay https://ray-project.github.io/kuberay-helm/ --force-update
helm repo update
# The chart normally gets this label from Node Feature Discovery. The verified
# single GPU node is labeled directly so NFD is not required on this cluster.
kctl label node --all nvidia.com/gpu.present=true --overwrite
helm upgrade --install nvdp nvdp/nvidia-device-plugin \
  --version "$NVIDIA_PLUGIN_VERSION" \
  --namespace nvidia-device-plugin --create-namespace \
  --set runtimeClassName=nvidia \
  --wait --timeout 10m

for _ in $(seq 1 60); do
  allocatable="$(kctl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}' 2>/dev/null || true)"
  [[ "$allocatable" =~ ^[1-9][0-9]*$ ]] && break
  sleep 5
done
[[ "${allocatable:-0}" =~ ^[1-9][0-9]*$ ]] || fail "Kubernetes did not expose nvidia.com/gpu"
echo "Allocatable GPUs: $allocatable"

echo "== GPU Kubernetes smoke test =="
kctl delete pod cuda-vector-add --ignore-not-found --wait=true
ray_ready="$(kctl -n repo-agent get rayservice repo-llm \
  -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
if [[ "$ray_ready" == "True" ]]; then
  echo "Skipping standalone CUDA pod: the ready RayService is using the single GPU"
else
  cat <<'EOF' | kctl apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: cuda-vector-add
spec:
  restartPolicy: Never
  runtimeClassName: nvidia
  containers:
    - name: cuda-vector-add
      image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda12.5.0
      resources:
        limits:
          nvidia.com/gpu: 1
EOF
  kctl wait --for=condition=PodScheduled pod/cuda-vector-add --timeout=3m
  for _ in $(seq 1 60); do
    phase="$(kctl get pod cuda-vector-add -o jsonpath='{.status.phase}')"
    [[ "$phase" == "Succeeded" || "$phase" == "Failed" ]] && break
    sleep 5
  done
  kctl logs cuda-vector-add
  [[ "$phase" == "Succeeded" ]] || fail "CUDA smoke test failed with phase $phase"
  kctl delete pod cuda-vector-add --wait=true
fi

echo "== KubeRay operator =="
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --version "$KUBERAY_VERSION" \
  --namespace ray-system --create-namespace \
  --wait --timeout 10m
kctl wait --for=condition=Established crd/rayservices.ray.io --timeout=3m

if [[ "$skip_monitoring" == "false" ]]; then
  echo "== Prometheus and Grafana =="
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
  helm repo update
  helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
    --version "$MONITORING_CHART_VERSION" \
    --namespace monitoring --create-namespace \
    --set alertmanager.enabled=false \
    --set prometheus.prometheusSpec.retention=3d \
    --set grafana.sidecar.dashboards.searchNamespace=ALL \
    --wait --timeout 15m
  kctl wait --for=condition=Established crd/servicemonitors.monitoring.coreos.com --timeout=3m
  kctl wait --for=condition=Established crd/podmonitors.monitoring.coreos.com --timeout=3m
fi

echo "== Build and import GraphServe image =="
image_archive=/tmp/repo-agent-dev.oci.tar
sudo rm -f "$image_archive"
# Export OCI directly to K3s. This avoids dependence on Docker's separate image
# snapshot store, which may be incomplete after an instance/filesystem restore.
sudo docker buildx build --pull --output "type=oci,dest=$image_archive" -t repo-agent:dev .
sudo k3s ctr images import "$image_archive"
sudo rm -f "$image_archive"

echo "== GraphServe secret and manifests =="
kctl create namespace repo-agent --dry-run=client -o yaml | kctl apply -f -
encoded_api_key="$(printf '%s' "$agent_api_key" | base64 | tr -d '\n')"
cat <<EOF | kctl apply -f -
apiVersion: v1
kind: Secret
metadata:
  name: agent-auth
  namespace: repo-agent
type: Opaque
data:
  api-key: ${encoded_api_key}
EOF

if [[ "$skip_monitoring" == "true" ]]; then
  kctl create configmap nginx-config --from-file=nginx.conf=deploy/k8s/nginx.conf \
    --namespace repo-agent --dry-run=client -o yaml | kctl apply -f -
  kctl apply -f deploy/k8s/storage.yaml
  kctl -n repo-agent apply -f deploy/k8s/application.yaml
  kctl -n repo-agent apply -f deploy/k8s/sie.yaml
  kctl -n repo-agent apply -f deploy/k8s/rayservice.yaml
else
  kctl apply -k deploy/k8s
  kctl apply -f deploy/k8s/grafana-dashboard.yaml
fi
# Secret environment variables are read only when a pod starts.
kctl -n repo-agent rollout restart deployment/agent

echo "== Wait for services =="
kctl -n repo-agent rollout status deployment/sie --timeout=15m
kctl -n repo-agent wait --for=condition=Ready rayservice/repo-llm --timeout=60m
kctl -n repo-agent rollout status deployment/agent --timeout=10m
kctl -n repo-agent rollout status deployment/gateway --timeout=10m
kctl -n repo-agent rollout status deployment/nginx --timeout=10m

echo "== Local VM access =="
if pgrep -f 'kubectl.*port-forward.*svc/nginx.*8080:8080' >/dev/null; then
  echo "GraphServe port-forward already running"
else
  nohup k3s kubectl -n repo-agent \
    port-forward --address 127.0.0.1 svc/nginx 8080:8080 \
    >/tmp/graphserve-port-forward.log 2>&1 &
fi

if [[ "$skip_monitoring" == "false" ]]; then
  if ! pgrep -f 'kubectl.*port-forward.*monitoring-grafana.*3000:80' >/dev/null; then
    nohup k3s kubectl -n monitoring \
      port-forward --address 127.0.0.1 svc/monitoring-grafana 3000:80 \
      >/tmp/grafana-port-forward.log 2>&1 &
  fi
  if ! pgrep -f 'kubectl.*port-forward.*monitoring-kube-prometheus-prometheus.*9090:9090' >/dev/null; then
    nohup k3s kubectl -n monitoring \
      port-forward --address 127.0.0.1 svc/monitoring-kube-prometheus-prometheus 9090:9090 \
      >/tmp/prometheus-port-forward.log 2>&1 &
  fi
fi

curl --fail --retry 20 --retry-connrefused --retry-delay 3 http://127.0.0.1:8080/healthz
echo
kctl -n repo-agent get pods,svc,rayservice,pvc
echo "GraphServe is ready on VM loopback port 8080."
