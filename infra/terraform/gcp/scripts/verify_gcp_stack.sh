#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-static}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
TF_DIR="${ROOT_DIR}/infra/terraform/gcp"

section() {
  printf "\n== %s ==\n" "$1"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

static_verify() {
  require_cmd helm

  section "Terraform fmt"
  if command -v terraform >/dev/null 2>&1; then
    terraform -chdir="${TF_DIR}" fmt -check -recursive
    if [ -d "${TF_DIR}/.terraform" ]; then
      terraform -chdir="${TF_DIR}" validate
    else
      echo "Skipping terraform validate because .terraform is not initialized."
    fi
  else
    echo "Skipping terraform fmt/validate because terraform is not installed."
  fi

  section "Helm render: Triton GPU serving"
  helm template recsys-serving "${ROOT_DIR}/infra/helm/recsys-serving" \
    --namespace kserve-triton-inference \
    -f "${ROOT_DIR}/infra/helm/recsys-serving/values-gcp-gpu.yaml" >/tmp/recsys-serving-gcp.yaml
  rg 'nvidia.com/gpu|cloud.google.com/gke-accelerator|kind: InferenceService|kind: ScaledObject' /tmp/recsys-serving-gcp.yaml

  section "Helm render: Ray GPU training"
  helm template recsys-ray-gpu "${ROOT_DIR}/infra/helm/ray-cluster" \
    --namespace kubeflow \
    -f "${ROOT_DIR}/infra/helm/ray-cluster/values-gcp-gpu.yaml" >/tmp/recsys-ray-gcp.yaml
  rg 'nvidia.com/gpu|cloud.google.com/gke-accelerator|--gpus-per-trial 1|kind: RayJob' /tmp/recsys-ray-gcp.yaml

  section "Helm render: core services"
  helm template recsys-data-platform "${ROOT_DIR}/infra/helm/recsys-data-platform" \
    --namespace recsys-dataflow \
    -f "${ROOT_DIR}/infra/helm/recsys-data-platform/values-gcp.yaml" >/dev/null
  helm template recsys-mlflow "${ROOT_DIR}/infra/helm/mlflow-stack" \
    --namespace experiment-tracking \
    -f "${ROOT_DIR}/infra/helm/mlflow-stack/values-gcp.yaml" >/dev/null
  helm template recsys-runtime "${ROOT_DIR}/infra/helm/recsys-runtime" \
    --namespace kubeflow \
    -f "${ROOT_DIR}/infra/helm/recsys-runtime/values-gcp.yaml" >/dev/null
  helm template recsys-observability "${ROOT_DIR}/infra/helm/recsys-observability" \
    --namespace observability \
    -f "${ROOT_DIR}/infra/helm/recsys-observability/values-gcp.yaml" >/dev/null

  section "Static verification passed"
}

live_verify() {
  require_cmd kubectl

  section "Nodes"
  kubectl get nodes -L cloud.google.com/gke-accelerator,recsys.ai/pool
  kubectl get nodes -l cloud.google.com/gke-accelerator --no-headers | awk 'END { if (NR < 1) exit 1 }'

  section "Core rollouts"
  kubectl rollout status deploy/ml-pipeline -n kubeflow --timeout=300s
  kubectl rollout status deploy/kuberay-operator -n kubeflow --timeout=300s
  kubectl rollout status deploy/mlflow -n experiment-tracking --timeout=300s
  kubectl rollout status deploy/minio -n experiment-tracking --timeout=300s
  kubectl rollout status deploy/postgres -n experiment-tracking --timeout=300s
  kubectl rollout status deploy/data-platform-minio -n recsys-dataflow --timeout=300s
  kubectl rollout status deploy/kafka -n recsys-dataflow --timeout=300s
  kubectl rollout status deploy/redis -n recsys-dataflow --timeout=300s
  kubectl rollout status deploy/airflow-webserver -n recsys-dataflow --timeout=300s
  kubectl rollout status deploy/recsys-online-feature-api -n api-serving --timeout=300s
  kubectl rollout status deploy/recsys-api-serving -n api-serving --timeout=300s
  kubectl rollout status deploy/recsys-prometheus -n observability --timeout=300s
  kubectl rollout status deploy/recsys-grafana -n observability --timeout=300s

  section "KServe and Triton"
  kubectl get clusterservingruntime kserve-tritonserver
  kubectl get inferenceservice recsys-bst-triton -n kserve-triton-inference
  kubectl get deploy -n kserve-triton-inference -l app=isvc.recsys-bst-triton-predictor -o wide || true
  kubectl get scaledobject -n kserve-triton-inference

  section "Ray"
  kubectl get rayjob -n kubeflow
  kubectl get pods -n kubeflow -l ray.io/node-type=worker -o wide || true

  section "API smoke"
  kubectl run recsys-online-feature-api-smoke \
    --rm -i --restart=Never \
    --image=curlimages/curl:8.10.1 \
    --namespace api-serving \
    --command -- curl -fsS http://recsys-online-feature-api.api-serving.svc.cluster.local/healthz

  kubectl run recsys-api-smoke \
    --rm -i --restart=Never \
    --image=curlimages/curl:8.10.1 \
    --namespace api-serving \
    --command -- curl -fsS http://recsys-api-serving.api-serving.svc.cluster.local/healthz

  section "Live verification passed"
}

case "${MODE}" in
  static)
    static_verify
    ;;
  live)
    live_verify
    ;;
  *)
    echo "Usage: $0 [static|live]" >&2
    exit 2
    ;;
esac
