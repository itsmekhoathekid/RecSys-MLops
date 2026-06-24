#!/usr/bin/env bash
set -euo pipefail

PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"
WAIT_TIMEOUT="${RECSYS_CLUSTER_DELETE_TIMEOUT:-300s}"
DELETE_PROFILE="${RECSYS_CLUSTER_DELETE_PROFILE:-0}"
DELETE_DATAHUB="${RECSYS_CLUSTER_DELETE_DATAHUB:-1}"

CORE_NAMESPACES=(
  api-serving
  observability
  recsys-dataflow
  experiment-tracking
  kubeflow
  kserve-triton-inference
  kserve
  ingress-nginx
  keda
)

section() {
  printf "\n== %s ==\n" "$1"
}

uninstall_release() {
  local release="$1"
  local namespace="$2"
  if helm status "${release}" -n "${namespace}" >/dev/null 2>&1; then
    helm uninstall "${release}" -n "${namespace}" || true
  else
    echo "Skipping Helm release ${namespace}/${release}: not installed"
  fi
}

delete_namespace() {
  local namespace="$1"
  if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    kubectl delete namespace "${namespace}" --wait=false
  else
    echo "Skipping namespace ${namespace}: not found"
  fi
}

delete_namespaced_resource_kind() {
  local namespace="$1"
  local resource="$2"
  if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    kubectl delete "${resource}" --all -n "${namespace}" --ignore-not-found --wait=false || true
  fi
}

clear_namespaced_resource_finalizers() {
  local namespace="$1"
  local resource="$2"
  if ! kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    return 0
  fi
  while IFS= read -r name; do
    [[ -z "${name}" ]] && continue
    kubectl patch "${resource}" "${name}" -n "${namespace}" --type=merge -p '{"metadata":{"finalizers":[]}}' || true
  done < <(kubectl get "${resource}" -n "${namespace}" -o name --ignore-not-found 2>/dev/null | sed 's#^.*/##')
}

delete_kserve_webhooks() {
  kubectl delete mutatingwebhookconfiguration inferenceservice.serving.kserve.io --ignore-not-found || true
  kubectl delete validatingwebhookconfiguration \
    clusterservingruntime.serving.kserve.io \
    inferencegraph.serving.kserve.io \
    inferenceservice.serving.kserve.io \
    localmodelcache.serving.kserve.io \
    servingruntime.serving.kserve.io \
    trainedmodel.serving.kserve.io \
    --ignore-not-found || true
}

wait_namespace_deleted() {
  local namespace="$1"
  if kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    kubectl wait --for=delete "namespace/${namespace}" --timeout="${WAIT_TIMEOUT}" || {
      echo "Namespace ${namespace} still exists after ${WAIT_TIMEOUT}"
      kubectl get namespace "${namespace}" || true
      return 1
    }
  fi
}

section "Select Minikube Context"
if minikube -p "${PROFILE}" status >/dev/null 2>&1; then
  kubectl config use-context "${PROFILE}" >/dev/null || true
else
  echo "Minikube profile ${PROFILE} is not running; checking/removing leftover Docker container only."
fi

if kubectl cluster-info >/dev/null 2>&1; then
  section "Uninstall Full Service Helm Releases"
  uninstall_release recsys-gateway api-serving
  uninstall_release ingress-nginx ingress-nginx
  uninstall_release recsys-serving kserve-triton-inference
  uninstall_release recsys-observability observability
  uninstall_release recsys-data-platform recsys-dataflow
  uninstall_release recsys-runtime kubeflow
  uninstall_release recsys-mlflow experiment-tracking
  uninstall_release kuberay-operator kubeflow
  uninstall_release keda-add-ons-http keda
  uninstall_release keda keda
  if [[ "${DELETE_DATAHUB}" == "1" ]]; then
    uninstall_release datahub datahub
    uninstall_release prerequisites datahub
  fi

  section "Delete Kept Custom Resources"
  clear_namespaced_resource_finalizers kserve-triton-inference inferenceservices.serving.kserve.io
  delete_namespaced_resource_kind kserve-triton-inference inferenceservices.serving.kserve.io
  clear_namespaced_resource_finalizers kubeflow rayjobs.ray.io
  delete_namespaced_resource_kind kubeflow rayjobs.ray.io
  clear_namespaced_resource_finalizers kubeflow rayclusters.ray.io
  delete_namespaced_resource_kind kubeflow rayclusters.ray.io
  delete_kserve_webhooks

  section "Delete Full Service Namespaces"
  for namespace in "${CORE_NAMESPACES[@]}"; do
    delete_namespace "${namespace}"
  done
  if [[ "${DELETE_DATAHUB}" == "1" ]]; then
    delete_namespace datahub
  fi

  section "Verify Services Removed"
  for namespace in "${CORE_NAMESPACES[@]}"; do
    wait_namespace_deleted "${namespace}"
  done
  if [[ "${DELETE_DATAHUB}" == "1" ]]; then
    wait_namespace_deleted datahub
  fi
else
  echo "Kubernetes API is not reachable; skipping Kubernetes resource cleanup."
fi

section "Stop Minikube"
if [[ "${DELETE_PROFILE}" == "1" ]]; then
  minikube -p "${PROFILE}" delete || true
else
  minikube -p "${PROFILE}" stop || true
fi

section "Cluster Status"
minikube -p "${PROFILE}" status || true

section "Docker Node Container"
if docker inspect "${PROFILE}" >/dev/null 2>&1; then
  docker inspect "${PROFILE}" --format 'memory_bytes={{.HostConfig.Memory}} memory_swap_bytes={{.HostConfig.MemorySwap}} state={{.State.Status}}'
else
  echo "Docker container ${PROFILE} not found"
fi
