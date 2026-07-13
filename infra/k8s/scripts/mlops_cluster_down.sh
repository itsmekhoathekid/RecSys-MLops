#!/usr/bin/env bash
set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"

section() {
  printf "\n== %s ==\n" "$1"
}

profile_host_state() {
  minikube -p "${PROFILE}" status --format='{{.Host}}' 2>/dev/null || true
}

verify_profile_stopped() {
  local host_state container_running
  host_state="$(profile_host_state)"
  if [[ -n "${host_state}" && "${host_state}" != "Stopped" && "${host_state}" != "Nonexistent" ]]; then
    echo "Minikube profile ${PROFILE} is not stopped: ${host_state}"
    return 1
  fi

  if docker inspect "${PROFILE}" >/dev/null 2>&1; then
    container_running="$(docker inspect "${PROFILE}" --format '{{.State.Running}}')"
    if [[ "${container_running}" != "false" ]]; then
      echo "Minikube Docker node ${PROFILE} is still running"
      return 1
    fi
  fi

  echo "Verified: profile ${PROFILE} is stopped and all Kubernetes pods are down."
}

section "Select Minikube Context"
if [[ "$(profile_host_state)" == "Running" ]]; then
  minikube -p "${PROFILE}" update-context >/dev/null
  kubectl config use-context "${PROFILE}" >/dev/null || true
  if [[ "$(kubectl config current-context 2>/dev/null || true)" != "${PROFILE}" ]]; then
    echo "Refusing to inspect Kubernetes resources: context is not ${PROFILE}"
    exit 1
  fi
else
  echo "Minikube profile ${PROFILE} is not running."
fi

if [[ "$(profile_host_state)" == "Running" ]] && kubectl --context "${PROFILE}" cluster-info >/dev/null 2>&1; then
  section "Preserve Data Volumes"
  echo "cluster-down is non-destructive: namespaces, PVCs, MinIO buckets, MLflow artifacts, and model weights are kept."
  kubectl --context "${PROFILE}" get pvc -A || true

  section "Current Full Service Namespaces"
  kubectl --context "${PROFILE}" get namespace \
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
if [[ -n "$(profile_host_state)" ]]; then
  minikube -p "${PROFILE}" stop
else
  echo "Minikube profile ${PROFILE} does not exist; nothing to stop."
fi

section "Verify All Pods Are Down"
verify_profile_stopped

section "Cluster Status"
minikube -p "${PROFILE}" status || true

section "Docker Node Container"
if docker inspect "${PROFILE}" >/dev/null 2>&1; then
  docker inspect "${PROFILE}" --format 'memory_bytes={{.HostConfig.Memory}} memory_swap_bytes={{.HostConfig.MemorySwap}} state={{.State.Status}}'
else
  echo "Docker container ${PROFILE} not found"
fi
