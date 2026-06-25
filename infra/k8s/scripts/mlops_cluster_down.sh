#!/usr/bin/env bash
set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"

section() {
  printf "\n== %s ==\n" "$1"
}

section "Select Minikube Context"
if minikube -p "${PROFILE}" status >/dev/null 2>&1; then
  kubectl config use-context "${PROFILE}" >/dev/null || true
else
  echo "Minikube profile ${PROFILE} is not running."
fi

if kubectl cluster-info >/dev/null 2>&1; then
  section "Preserve Data Volumes"
  echo "cluster-down is non-destructive: namespaces, PVCs, MinIO buckets, MLflow artifacts, and model weights are kept."
  kubectl get pvc -A || true

  section "Current Full Service Namespaces"
  kubectl get namespace \
    api-serving \
    observability \
    recsys-dataflow \
    experiment-tracking \
    kubeflow \
    kserve-triton-inference \
    kserve \
    ingress-nginx \
    keda \
    datahub \
    --ignore-not-found || true
else
  echo "Kubernetes API is not reachable; skipping service/PVC summary."
fi

section "Stop Minikube"
minikube -p "${PROFILE}" stop || true

section "Cluster Status"
minikube -p "${PROFILE}" status || true

section "Docker Node Container"
if docker inspect "${PROFILE}" >/dev/null 2>&1; then
  docker inspect "${PROFILE}" --format 'memory_bytes={{.HostConfig.Memory}} memory_swap_bytes={{.HostConfig.MemorySwap}} state={{.State.Status}}'
else
  echo "Docker container ${PROFILE} not found"
fi
