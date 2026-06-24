#!/usr/bin/env bash
set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"

section() {
  printf "\n== %s ==\n" "$1"
}

section "Minikube"
minikube -p "${PROFILE}" status || true

section "Minikube Config"
minikube -p "${PROFILE}" config view || true

section "Docker Node Container"
if docker inspect "${PROFILE}" >/dev/null 2>&1; then
  docker inspect "${PROFILE}" --format 'memory_bytes={{.HostConfig.Memory}} memory_swap_bytes={{.HostConfig.MemorySwap}} state={{.State.Status}}'
  docker stats --no-stream "${PROFILE}" || true
else
  echo "Docker container ${PROFILE} not found"
fi

if kubectl config current-context >/dev/null 2>&1 && kubectl get node "${PROFILE}" >/dev/null 2>&1; then
  section "Kubernetes Node"
  kubectl describe node "${PROFILE}" | rg 'Capacity:|Allocatable:|MemoryPressure|memory|cpu' || true

  section "Service Pods"
  for namespace in kubeflow experiment-tracking recsys-dataflow kserve kserve-triton-inference api-serving observability ingress-nginx keda datahub; do
    if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
      echo "--- ${namespace}"
      kubectl get pods -n "${namespace}"
    fi
  done
else
  section "Kubernetes Node"
  echo "Kubernetes API is not reachable for profile ${PROFILE}"
fi
