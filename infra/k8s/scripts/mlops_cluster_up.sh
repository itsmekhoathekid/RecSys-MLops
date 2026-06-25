#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"
CPUS="${MINIKUBE_CPUS:-8}"
MEMORY_MB="${MINIKUBE_MEMORY_MB:-16384}"
DISK_SIZE="${MINIKUBE_DISK_SIZE:-40g}"
WAIT_TIMEOUT="${RECSYS_CLUSTER_WAIT_TIMEOUT:-600s}"
KFP_VERSION="${KFP_VERSION:-2.16.1}"
KSERVE_VERSION="${KSERVE_VERSION:-v0.15.2}"
CERT_MANAGER_VERSION="${CERT_MANAGER_VERSION:-v1.16.2}"
BUILD_IMAGES="${RECSYS_CLUSTER_BUILD_IMAGES:-0}"
INSTALL_DATAHUB="${RECSYS_CLUSTER_INSTALL_DATAHUB:-0}"
SCALE_OPTIONAL_KFP="${RECSYS_CLUSTER_SCALE_OPTIONAL_KFP:-1}"
SECURITY_ENABLED="${RECSYS_CLUSTER_SECURITY_ENABLED:-1}"

TARGET_NAMESPACES=(
  kubeflow
  experiment-tracking
  recsys-dataflow
  kserve
  kserve-triton-inference
  api-serving
  observability
  ingress-nginx
  keda
  external-secrets
  vault
  istio-system
  recsys-security
)

REQUIRED_DEPLOYMENTS=(
  "kubeflow/ml-pipeline"
  "kubeflow/ml-pipeline-ui"
  "kubeflow/workflow-controller"
  "kubeflow/kuberay-operator"
  "experiment-tracking/mlflow"
  "experiment-tracking/minio"
  "experiment-tracking/postgres"
  "recsys-dataflow/data-platform-minio"
  "recsys-dataflow/kafka"
  "recsys-dataflow/kafka-connect"
  "recsys-dataflow/redis"
  "recsys-dataflow/airflow-webserver"
  "api-serving/recsys-api-serving"
  "observability/recsys-prometheus"
  "observability/recsys-grafana"
  "observability/recsys-loki"
  "observability/recsys-tempo"
  "observability/recsys-pushgateway"
  "ingress-nginx/ingress-nginx-controller"
  "keda/keda-operator"
  "keda/keda-add-ons-http-external-scaler"
  "keda/keda-add-ons-http-interceptor"
)

REQUIRED_SERVICES=(
  "kubeflow/ml-pipeline-ui"
  "kubeflow/ml-pipeline"
  "experiment-tracking/mlflow"
  "experiment-tracking/minio"
  "experiment-tracking/postgres"
  "recsys-dataflow/data-platform-minio"
  "recsys-dataflow/kafka"
  "recsys-dataflow/redis"
  "api-serving/recsys-api-serving"
  "observability/recsys-grafana"
  "observability/recsys-prometheus"
  "observability/recsys-loki"
  "observability/recsys-tempo"
  "keda/keda-add-ons-http-interceptor-proxy"
)

section() {
  printf "\n== %s ==\n" "$1"
}

run_make() {
  make -C "${ROOT_DIR}" "$@"
}

helm_uninstall_if_failed() {
  local release="$1"
  local namespace="$2"
  if helm status "${release}" -n "${namespace}" >/dev/null 2>&1; then
    local status
    status="$(helm status "${release}" -n "${namespace}" -o json | sed -n 's/.*"status":"\([^"]*\)".*/\1/p' | head -n 1 || true)"
    if [[ "${status}" == "failed" || "${status}" == "pending-install" || "${status}" == "pending-upgrade" ]]; then
      helm uninstall "${release}" -n "${namespace}" || true
    fi
  fi
}

kubectl_apply_server_side_with_retries() {
  local manifest="$1"
  local attempts="${2:-5}"
  for attempt in $(seq 1 "${attempts}"); do
    if kubectl apply --server-side --force-conflicts -f "${manifest}"; then
      return 0
    fi
    echo "Retrying server-side apply for ${manifest} (${attempt}/${attempts}) after webhook/API readiness delay"
    sleep 15
  done
  kubectl apply --server-side --force-conflicts -f "${manifest}"
}

install_kfp_if_needed() {
  if kubectl get deploy -n kubeflow ml-pipeline >/dev/null 2>&1; then
    echo "Kubeflow Pipelines already installed"
    return
  fi

  kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=${KFP_VERSION}"
  kubectl wait --for condition=established --timeout=60s crd/applications.app.k8s.io
  kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/env/dev?ref=${KFP_VERSION}"
}

install_kuberay_if_needed() {
  if kubectl get deploy -n kubeflow kuberay-operator >/dev/null 2>&1; then
    echo "KubeRay operator already installed"
    return
  fi

  helm repo add kuberay https://ray-project.github.io/kuberay-helm/ >/dev/null
  helm repo update kuberay >/dev/null
  helm upgrade --install kuberay-operator kuberay/kuberay-operator --namespace kubeflow --create-namespace
}

scale_optional_kfp_components() {
  if [[ "${SCALE_OPTIONAL_KFP}" != "1" ]]; then
    return
  fi
  if kubectl get namespace kubeflow >/dev/null 2>&1; then
    for deployment in metadata-writer proxy-agent; do
      if kubectl get deploy "${deployment}" -n kubeflow >/dev/null 2>&1; then
        kubectl scale deploy "${deployment}" -n kubeflow --replicas=0
      fi
    done
  fi
}

install_keda_if_needed() {
  if kubectl get crd scaledobjects.keda.sh >/dev/null 2>&1 \
    && kubectl get crd httpscaledobjects.http.keda.sh >/dev/null 2>&1 \
    && kubectl get deploy -n keda keda-operator >/dev/null 2>&1 \
    && kubectl get deploy -n keda keda-add-ons-http-external-scaler >/dev/null 2>&1; then
    echo "KEDA and KEDA HTTP add-on already installed"
    return
  fi

  helm repo add kedacore https://kedacore.github.io/charts >/dev/null
  helm repo update kedacore >/dev/null
  helm_uninstall_if_failed keda keda
  helm_uninstall_if_failed keda-add-ons-http keda
  helm upgrade --install keda kedacore/keda --namespace keda --create-namespace --wait --timeout 5m
  helm upgrade --install keda-add-ons-http kedacore/keda-add-ons-http --namespace keda --wait --timeout 5m
}

install_cert_manager_if_needed() {
  if kubectl get crd certificates.cert-manager.io >/dev/null 2>&1 \
    && kubectl get deploy -n cert-manager cert-manager >/dev/null 2>&1 \
    && kubectl get deploy -n cert-manager cert-manager-webhook >/dev/null 2>&1 \
    && kubectl get deploy -n cert-manager cert-manager-cainjector >/dev/null 2>&1; then
    echo "cert-manager already installed"
  else
    kubectl apply -f "https://github.com/cert-manager/cert-manager/releases/download/${CERT_MANAGER_VERSION}/cert-manager.yaml"
    kubectl wait --for condition=established --timeout=120s crd/certificates.cert-manager.io
    kubectl wait --for condition=established --timeout=120s crd/issuers.cert-manager.io
  fi
  kubectl rollout status deploy/cert-manager -n cert-manager --timeout="${WAIT_TIMEOUT}"
  kubectl rollout status deploy/cert-manager-webhook -n cert-manager --timeout="${WAIT_TIMEOUT}"
  kubectl rollout status deploy/cert-manager-cainjector -n cert-manager --timeout="${WAIT_TIMEOUT}"
  for _ in $(seq 1 60); do
    if kubectl get endpoints cert-manager-webhook -n cert-manager \
      -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null | grep -q .; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for cert-manager webhook endpoints"
  kubectl get endpoints cert-manager-webhook -n cert-manager || true
  return 1
}

wait_kserve_webhook() {
  kubectl rollout status deploy/kserve-controller-manager -n kserve --timeout="${WAIT_TIMEOUT}"
  for _ in $(seq 1 60); do
    if kubectl get endpoints kserve-webhook-server-service -n kserve \
      -o jsonpath='{.subsets[0].addresses[0].ip}' 2>/dev/null | grep -q .; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for KServe webhook endpoints"
  kubectl get endpoints kserve-webhook-server-service -n kserve || true
  return 1
}

install_kserve_if_needed() {
  if kubectl get crd inferenceservices.serving.kserve.io >/dev/null 2>&1 \
    && kubectl get deploy -n kserve kserve-controller-manager >/dev/null 2>&1 \
    && kubectl get mutatingwebhookconfiguration inferenceservice.serving.kserve.io >/dev/null 2>&1 \
    && kubectl get certificate serving-cert -n kserve >/dev/null 2>&1 \
    && kubectl get clusterservingruntime kserve-tritonserver >/dev/null 2>&1; then
    echo "KServe CRDs and controller already installed"
  else
    kubectl_apply_server_side_with_retries "https://github.com/kserve/kserve/releases/download/${KSERVE_VERSION}/kserve.yaml"
    wait_kserve_webhook
    kubectl_apply_server_side_with_retries "https://github.com/kserve/kserve/releases/download/${KSERVE_VERSION}/kserve-cluster-resources.yaml"
  fi
}

deployment_selector() {
  local namespace="$1"
  local deployment="$2"
  kubectl get deploy "${deployment}" -n "${namespace}" -o go-template='{{range $k,$v := .spec.selector.matchLabels}}{{printf "%s=%s," $k $v}}{{end}}' | sed 's/,$//'
}

ensure_deployment_available() {
  local namespace="$1"
  local deployment="$2"
  if ! kubectl get deploy "${deployment}" -n "${namespace}" >/dev/null 2>&1; then
    echo "Skipping ${namespace}/${deployment}: deployment not installed"
    return 0
  fi

  if kubectl rollout status "deploy/${deployment}" -n "${namespace}" --timeout=120s; then
    return 0
  fi

  echo "Restarting ${namespace}/${deployment}: rollout was not available"
  kubectl rollout restart "deploy/${deployment}" -n "${namespace}" || true
  local selector
  selector="$(deployment_selector "${namespace}" "${deployment}" || true)"
  if [[ -n "${selector}" ]]; then
    kubectl delete pod -n "${namespace}" -l "${selector}" --ignore-not-found --wait=false || true
  fi
  kubectl rollout status "deploy/${deployment}" -n "${namespace}" --timeout="${WAIT_TIMEOUT}"
}

ensure_dependency_rollouts() {
  ensure_deployment_available keda keda-operator
  ensure_deployment_available keda keda-operator-metrics-apiserver
  ensure_deployment_available keda keda-add-ons-http-external-scaler
  ensure_deployment_available keda keda-add-ons-http-interceptor
  ensure_deployment_available kserve kserve-controller-manager
  ensure_deployment_available kserve kserve-localmodel-controller-manager
}

wait_rollouts_in_namespace() {
  local namespace="$1"
  if ! kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    echo "Skipping namespace ${namespace}: not installed"
    return 0
  fi

  echo "--- ${namespace}: deployments"
  while IFS= read -r resource; do
    [[ -z "${resource}" ]] && continue
    kubectl -n "${namespace}" rollout status "${resource}" --timeout="${WAIT_TIMEOUT}"
  done < <(kubectl -n "${namespace}" get deploy -o name)

  echo "--- ${namespace}: statefulsets"
  while IFS= read -r resource; do
    [[ -z "${resource}" ]] && continue
    kubectl -n "${namespace}" rollout status "${resource}" --timeout="${WAIT_TIMEOUT}"
  done < <(kubectl -n "${namespace}" get statefulset -o name)
}

verify_required_deployment() {
  local namespace="${1%%/*}"
  local workload="${1##*/}"
  if kubectl get deploy "${workload}" -n "${namespace}" >/dev/null 2>&1; then
    kubectl rollout status "deploy/${workload}" -n "${namespace}" --timeout="${WAIT_TIMEOUT}"
    return
  fi
  if kubectl get statefulset "${workload}" -n "${namespace}" >/dev/null 2>&1; then
    kubectl rollout status "statefulset/${workload}" -n "${namespace}" --timeout="${WAIT_TIMEOUT}"
    return
  fi
  echo "Required workload ${namespace}/${workload} not found as Deployment or StatefulSet"
  return 1
}

verify_required_service() {
  local namespace="${1%%/*}"
  local service="${1##*/}"
  kubectl get svc "${service}" -n "${namespace}" >/dev/null
}

section "Start Minikube"
minikube start \
  --profile "${PROFILE}" \
  --driver=docker \
  --cpus="${CPUS}" \
  --memory="${MEMORY_MB}" \
  --disk-size="${DISK_SIZE}"

kubectl config use-context "${PROFILE}"

section "Enforce Docker Container Memory"
if docker inspect "${PROFILE}" >/dev/null 2>&1; then
  docker update --memory "${MEMORY_MB}m" --memory-swap "${MEMORY_MB}m" "${PROFILE}" >/dev/null
  docker inspect "${PROFILE}" --format 'memory_bytes={{.HostConfig.Memory}} memory_swap_bytes={{.HostConfig.MemorySwap}} state={{.State.Status}}'
else
  echo "Docker container ${PROFILE} not found after minikube start"
fi

section "Wait Node"
kubectl wait --for=condition=Ready "node/${PROFILE}" --timeout=240s

if [[ "${BUILD_IMAGES}" == "1" ]]; then
  section "Build Local Images In Minikube"
  run_make mlops-images-minikube
  run_make data-platform-images-minikube
else
  section "Build Local Images In Minikube"
  echo "Skipping image build. Set RECSYS_CLUSTER_BUILD_IMAGES=1 to rebuild local images before install."
fi

section "Install Cluster Dependencies"
install_keda_if_needed
install_cert_manager_if_needed
install_kserve_if_needed
ensure_dependency_rollouts

if [[ "${SECURITY_ENABLED}" == "1" ]]; then
  section "Install Security Stack"
  run_make security-install
else
  section "Install Security Stack"
  echo "Skipping security stack. Set RECSYS_CLUSTER_SECURITY_ENABLED=1 to enable Vault/ESO/Istio."
fi

section "Install Kubeflow And KubeRay"
install_kfp_if_needed
install_kuberay_if_needed
scale_optional_kfp_components

section "Install Core RecSys Services"
run_make observability-install
run_make mlops-install-stack
run_make data-platform-install
run_make mlops-install-serving
run_make gateway-install-controller
run_make gateway-install
if [[ "${INSTALL_DATAHUB}" == "1" ]]; then
  run_make datahub-install
fi

section "Wait Full Service Rollouts"
for namespace in "${TARGET_NAMESPACES[@]}"; do
  wait_rollouts_in_namespace "${namespace}"
done
if [[ "${INSTALL_DATAHUB}" == "1" ]]; then
  wait_rollouts_in_namespace datahub
fi

section "Wait KServe InferenceService"
if kubectl get inferenceservice recsys-bst-triton -n kserve-triton-inference >/dev/null 2>&1; then
  kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton -n kserve-triton-inference --timeout="${WAIT_TIMEOUT}" || {
    echo "InferenceService is not Ready yet; continuing because model storage may be populated by the promotion flow."
    kubectl get inferenceservice recsys-bst-triton -n kserve-triton-inference
  }
else
  echo "Skipping InferenceService wait: recsys-bst-triton not installed"
fi

section "Verify Required Deployments"
for resource in "${REQUIRED_DEPLOYMENTS[@]}"; do
  verify_required_deployment "${resource}"
done

section "Verify Required Services"
for resource in "${REQUIRED_SERVICES[@]}"; do
  verify_required_service "${resource}"
done

section "Final Status"
"$(dirname "$0")/mlops_cluster_status.sh"
