#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-}"

PROJECT_ID="${GCP_PROJECT_ID:-${PROJECT_ID:-fsds-coursework}}"
ZONE="${GKE_ZONE:-${ZONE:-asia-southeast1-b}}"
CLUSTER="${GKE_CLUSTER:-${CLUSTER:-recsys-mlops-gke}}"
STATE_FILE="${GCP_POWER_STATE_FILE:-.gcp-services-power-state.env}"
WAIT_TIMEOUT="${GCP_SERVICES_WAIT_TIMEOUT:-900s}"
SKIP_SMOKE="${GCP_SERVICES_SKIP_SMOKE:-0}"

CPU_NODE_POOL="${GCP_CPU_NODE_POOL:-recsys-mlops-cpu}"
ML_NODE_POOL="${GCP_ML_NODE_POOL:-recsys-mlops-ml-system}"
GPU_NODE_POOL="${GCP_GPU_NODE_POOL:-recsys-mlops-gpu}"

DEFAULT_CPU_NODES="${GCP_CPU_NODES:-1}"
DEFAULT_CPU_MIN_NODES="${GCP_CPU_MIN_NODES:-${DEFAULT_CPU_NODES}}"
DEFAULT_CPU_MAX_NODES="${GCP_CPU_MAX_NODES:-3}"
DEFAULT_ML_NODES="${GCP_ML_NODES:-1}"
DEFAULT_ML_MIN_NODES="${GCP_ML_MIN_NODES:-${DEFAULT_ML_NODES}}"
DEFAULT_ML_MAX_NODES="${GCP_ML_MAX_NODES:-1}"
DEFAULT_GPU_NODES="${GCP_GPU_NODES:-0}"
DEFAULT_GPU_MIN_NODES="${GCP_GPU_MIN_NODES:-${DEFAULT_GPU_NODES}}"
DEFAULT_GPU_MAX_NODES="${GCP_GPU_MAX_NODES:-1}"

usage() {
  cat <<USAGE
Usage:
  $0 down      Scale GKE node pools to 0 and keep PVC/PV data.
  $0 up        Restore node pools, wait services Ready, and run smoke checks.
  $0 status    Print node pools, PVCs, and non-running pods.

Environment overrides:
  GCP_PROJECT_ID=${PROJECT_ID}
  GKE_ZONE=${ZONE}
  GKE_CLUSTER=${CLUSTER}
  GCP_CPU_NODES=${DEFAULT_CPU_NODES}
  GCP_ML_NODES=${DEFAULT_ML_NODES}
  GCP_GPU_NODES=${DEFAULT_GPU_NODES}
  GCP_SERVICES_SKIP_SMOKE=${SKIP_SMOKE}

State file:
  ${STATE_FILE}
USAGE
}

require_tools() {
  command -v gcloud >/dev/null
  command -v kubectl >/dev/null
}

cluster_args() {
  printf -- '--project=%s --zone=%s' "${PROJECT_ID}" "${ZONE}"
}

get_credentials() {
  gcloud container clusters get-credentials "${CLUSTER}" --zone "${ZONE}" --project "${PROJECT_ID}" >/dev/null
}

pool_exists() {
  local pool="$1"
  gcloud container node-pools describe "${pool}" \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" >/dev/null 2>&1
}

pool_value() {
  local pool="$1"
  local expr="$2"
  gcloud container node-pools describe "${pool}" \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --format="value(${expr})" 2>/dev/null || true
}

safe_int() {
  local value="$1"
  local fallback="$2"
  if [[ "${value}" =~ ^[0-9]+$ ]]; then
    printf '%s' "${value}"
  else
    printf '%s' "${fallback}"
  fi
}

record_pool_state() {
  local key="$1"
  local pool="$2"
  local default_nodes="$3"
  local default_min="$4"
  local default_max="$5"

  if ! pool_exists "${pool}"; then
    printf '%s_EXISTS=0\n' "${key}" >>"${STATE_FILE}"
    return 0
  fi

  local current min max
  current="$(safe_int "$(pool_value "${pool}" "currentNodeCount")" "${default_nodes}")"
  min="$(safe_int "$(pool_value "${pool}" "autoscaling.minNodeCount")" "${default_min}")"
  max="$(safe_int "$(pool_value "${pool}" "autoscaling.maxNodeCount")" "${default_max}")"

  if (( max < min )); then
    max="${min}"
  fi
  if (( max < current )); then
    max="${current}"
  fi

  {
    printf '%s_EXISTS=1\n' "${key}"
    printf '%s_POOL=%q\n' "${key}" "${pool}"
    printf '%s_NODES=%q\n' "${key}" "${current}"
    printf '%s_MIN=%q\n' "${key}" "${min}"
    printf '%s_MAX=%q\n' "${key}" "${max}"
  } >>"${STATE_FILE}"
}

write_state() {
  {
    printf 'PROJECT_ID=%q\n' "${PROJECT_ID}"
    printf 'ZONE=%q\n' "${ZONE}"
    printf 'CLUSTER=%q\n' "${CLUSTER}"
    printf 'RECORDED_AT=%q\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"${STATE_FILE}"
  record_pool_state CPU "${CPU_NODE_POOL}" "${DEFAULT_CPU_NODES}" "${DEFAULT_CPU_MIN_NODES}" "${DEFAULT_CPU_MAX_NODES}"
  record_pool_state ML "${ML_NODE_POOL}" "${DEFAULT_ML_NODES}" "${DEFAULT_ML_MIN_NODES}" "${DEFAULT_ML_MAX_NODES}"
  record_pool_state GPU "${GPU_NODE_POOL}" "${DEFAULT_GPU_NODES}" "${DEFAULT_GPU_MIN_NODES}" "${DEFAULT_GPU_MAX_NODES}"
}

set_pool_autoscaling() {
  local pool="$1"
  local min="$2"
  local max="$3"

  if (( max < min )); then
    max="${min}"
  fi
  if (( max < 1 )); then
    max=1
  fi

  gcloud container node-pools update "${pool}" \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --enable-autoscaling \
    --min-nodes "${min}" \
    --max-nodes "${max}" \
    --quiet
}

resize_pool() {
  local pool="$1"
  local nodes="$2"

  gcloud container clusters resize "${CLUSTER}" \
    --node-pool "${pool}" \
    --num-nodes "${nodes}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --quiet
}

scale_pool_down() {
  local label="$1"
  local pool="$2"

  if ! pool_exists "${pool}"; then
    echo "Skip ${label}: node pool ${pool} does not exist."
    return 0
  fi

  local max
  max="$(safe_int "$(pool_value "${pool}" "autoscaling.maxNodeCount")" "1")"
  echo "Hibernate ${label}: ${pool} -> min=0, nodes=0"
  set_pool_autoscaling "${pool}" 0 "${max}"
  resize_pool "${pool}" 0
}

scale_pool_up() {
  local label="$1"
  local pool="$2"
  local nodes="$3"
  local min="$4"
  local max="$5"

  if ! pool_exists "${pool}"; then
    echo "Skip ${label}: node pool ${pool} does not exist."
    return 0
  fi

  if (( nodes < min )); then
    nodes="${min}"
  fi
  if (( max < nodes )); then
    max="${nodes}"
  fi

  echo "Resume ${label}: ${pool} -> min=${min}, max=${max}, nodes=${nodes}"
  set_pool_autoscaling "${pool}" "${min}" "${max}"
  resize_pool "${pool}" "${nodes}"
}

load_state_or_defaults() {
  CPU_EXISTS=1
  CPU_POOL="${CPU_NODE_POOL}"
  CPU_NODES="${DEFAULT_CPU_NODES}"
  CPU_MIN="${DEFAULT_CPU_MIN_NODES}"
  CPU_MAX="${DEFAULT_CPU_MAX_NODES}"

  ML_EXISTS=1
  ML_POOL="${ML_NODE_POOL}"
  ML_NODES="${DEFAULT_ML_NODES}"
  ML_MIN="${DEFAULT_ML_MIN_NODES}"
  ML_MAX="${DEFAULT_ML_MAX_NODES}"

  GPU_EXISTS=1
  GPU_POOL="${GPU_NODE_POOL}"
  GPU_NODES="${DEFAULT_GPU_NODES}"
  GPU_MIN="${DEFAULT_GPU_MIN_NODES}"
  GPU_MAX="${DEFAULT_GPU_MAX_NODES}"

  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${STATE_FILE}"
  fi
}

print_status() {
  get_credentials
  echo "== Node pools =="
  gcloud container node-pools list \
    --cluster "${CLUSTER}" \
    --zone "${ZONE}" \
    --project "${PROJECT_ID}" \
    --format='table(name,status,autoscaling.enabled,autoscaling.minNodeCount,autoscaling.maxNodeCount,version)'
  echo
  echo "== Nodes =="
  kubectl get nodes -L cloud.google.com/gke-nodepool,recsys.ai/workload || true
  echo
  echo "== PVCs kept =="
  kubectl get pvc -A || true
  echo
  echo "== Pods not Running/Succeeded =="
  kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded || true
}

wait_rollout_all() {
  local namespace="$1"

  if ! kubectl get namespace "${namespace}" >/dev/null 2>&1; then
    return 0
  fi

  echo "Wait rollouts in namespace ${namespace}"
  local kind
  local -a resources
  for kind in deployment statefulset daemonset; do
    mapfile -t resources < <(kubectl get "${kind}" -n "${namespace}" -o name 2>/dev/null || true)
    if ((${#resources[@]} > 0)); then
      kubectl rollout status -n "${namespace}" --timeout="${WAIT_TIMEOUT}" "${resources[@]}"
    fi
  done
}

wait_ready_after_up() {
  get_credentials

  if (( CPU_NODES > 0 )); then
    kubectl wait --for=condition=Ready node \
      -l "cloud.google.com/gke-nodepool=${CPU_POOL}" \
      --timeout="${WAIT_TIMEOUT}"
  fi
  if (( ML_NODES > 0 )); then
    kubectl wait --for=condition=Ready node \
      -l "cloud.google.com/gke-nodepool=${ML_POOL}" \
      --timeout="${WAIT_TIMEOUT}"
  fi
  if (( GPU_NODES > 0 )) && pool_exists "${GPU_POOL}"; then
    kubectl wait --for=condition=Ready node \
      -l "cloud.google.com/gke-nodepool=${GPU_POOL}" \
      --timeout="${WAIT_TIMEOUT}" || true
  fi

  local namespaces=(
    cert-manager
    external-secrets
    istio-system
    keda
    kserve
    ingress-nginx
    experiment-tracking
    recsys-dataflow
    kubeflow
    kserve-triton-inference
    api-serving
    observability
    datahub
  )
  for namespace in "${namespaces[@]}"; do
    wait_rollout_all "${namespace}"
  done

  if kubectl get inferenceservice -n kserve-triton-inference recsys-bst-triton >/dev/null 2>&1; then
    kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton \
      -n kserve-triton-inference \
      --timeout="${WAIT_TIMEOUT}"
  fi
  if kubectl get inferenceservice -n kserve-triton-inference recsys-bst-triton-candidate >/dev/null 2>&1; then
    kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton-candidate \
      -n kserve-triton-inference \
      --timeout="${WAIT_TIMEOUT}" || true
  fi
}

smoke_after_up() {
  if [[ "${SKIP_SMOKE}" == "1" ]]; then
    echo "Skip smoke checks because GCP_SERVICES_SKIP_SMOKE=1."
    return 0
  fi

  echo "== Smoke: no Pending/Failed pods =="
  kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded

  if kubectl get deploy -n api-serving recsys-api-serving >/dev/null 2>&1; then
    echo "== Smoke: recommendation API =="
    kubectl exec -n api-serving deploy/recsys-api-serving -c api -- \
      python -c 'import requests; body={"user_id":1001,"candidate_item_ids":[1,2,3,4,5,6,7,8,9,10],"top_k":5}; r=requests.post("http://127.0.0.1:8080/recommendations", json=body, timeout=30); print(r.status_code); print(r.text[:500]); r.raise_for_status()'
  fi

  if kubectl get deploy -n recsys-dataflow flink-jobmanager >/dev/null 2>&1; then
    echo "== Smoke: Flink overview =="
    kubectl exec -n recsys-dataflow deploy/flink-jobmanager -- \
      curl -s http://localhost:8081/jobs/overview || true
    echo
  fi
}

hibernate_down() {
  get_credentials
  echo "Recording live node-pool state to ${STATE_FILE}"
  write_state
  echo "PVC/PV data will be kept. This command does not delete namespaces, Helm releases, PVCs, or PVs."
  kubectl get pvc -A || true
  scale_pool_down CPU "${CPU_NODE_POOL}"
  scale_pool_down ML "${ML_NODE_POOL}"
  scale_pool_down GPU "${GPU_NODE_POOL}"
  echo "GCP services are hibernating. Run '$0 up' to restore node pools and wait services Ready."
}

resume_up() {
  load_state_or_defaults
  get_credentials

  if [[ "${CPU_EXISTS:-1}" == "1" ]]; then
    scale_pool_up CPU "${CPU_POOL}" "${CPU_NODES}" "${CPU_MIN}" "${CPU_MAX}"
  fi
  if [[ "${ML_EXISTS:-1}" == "1" ]]; then
    scale_pool_up ML "${ML_POOL}" "${ML_NODES}" "${ML_MIN}" "${ML_MAX}"
  fi
  if [[ "${GPU_EXISTS:-1}" == "1" ]]; then
    scale_pool_up GPU "${GPU_POOL}" "${GPU_NODES}" "${GPU_MIN}" "${GPU_MAX}"
  fi

  wait_ready_after_up
  smoke_after_up
  echo "GCP services are back up and PVC-backed data was preserved."
}

require_tools

case "${ACTION}" in
  down)
    hibernate_down
    ;;
  up)
    resume_up
    ;;
  status)
    print_status
    ;;
  ""|-h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
