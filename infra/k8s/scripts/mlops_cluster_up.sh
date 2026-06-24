#!/usr/bin/env bash
set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"
CPUS="${MINIKUBE_CPUS:-8}"
MEMORY_MB="${MINIKUBE_MEMORY_MB:-16384}"
DISK_SIZE="${MINIKUBE_DISK_SIZE:-40g}"
WAIT_TIMEOUT="${RECSYS_CLUSTER_WAIT_TIMEOUT:-600s}"
TARGET_NAMESPACES=(
  kubeflow
  experiment-tracking
  recsys-dataflow
  kserve-triton-inference
  api-serving
  keda
)

section() {
  printf "\n== %s ==\n" "$1"
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

section "Wait Full Service Rollouts"
for namespace in "${TARGET_NAMESPACES[@]}"; do
  wait_rollouts_in_namespace "${namespace}"
done

section "Wait KServe InferenceService"
if kubectl get inferenceservice recsys-bst-triton -n kserve-triton-inference >/dev/null 2>&1; then
  kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton -n kserve-triton-inference --timeout="${WAIT_TIMEOUT}"
else
  echo "Skipping InferenceService wait: recsys-bst-triton not installed"
fi

section "Final Status"
"$(dirname "$0")/mlops_cluster_status.sh"
