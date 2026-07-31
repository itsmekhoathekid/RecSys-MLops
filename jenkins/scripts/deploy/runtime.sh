#!/usr/bin/env bash

stop_runtime_port_forwards() {
  local pid
  for pid in "${kfp_port_forward_pids[@]:-}"; do
    kill "${pid}" >/dev/null 2>&1 || true
  done
}

resolve_release_image() {
  local immutable_ref
  immutable_ref="$(image_manifest_lookup "$1")"
  if [[ -n "${immutable_ref}" ]]; then
    printf '%s' "${immutable_ref}"
  else
    printf '%s/%s:%s' "${image_registry}" "$1" "${image_tag}"
  fi
}

wait_for_local_port() {
  local port="$1"
  local label="$2"
  for _ in $(seq 1 60); do
    if (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  recsys_error "timed out waiting for ${label} on 127.0.0.1:${port}"
  return 1
}

open_kfp_upload_endpoint() {
  local endpoint="${KFP_ENDPOINT:-http://ml-pipeline.kubeflow.svc.cluster.local:8888}"
  local local_port="${KFP_LOCAL_PORT:-18888}"
  local log_dir="${JENKINS_HOME:-/tmp}/ci-tmp"
  local log_path="${log_dir}/recsys-kfp-upload-port-forward.log"
  mkdir -p "${log_dir}"

  if [[ "${endpoint}" != *".svc.cluster.local"* ]]; then
    kfp_upload_endpoint_result="${endpoint}"
    return 0
  fi

  (
    exec kubectl port-forward -n "${namespace_kubeflow}" svc/ml-pipeline "${local_port}:8888"
  ) >"${log_path}" 2>&1 9>&- &
  kfp_port_forward_pids+=("$!")
  wait_for_local_port "${local_port}" "Kubeflow Pipelines upload endpoint" || {
    cat "${log_path}" >&2 || true
    return 1
  }
  kfp_upload_endpoint_result="http://127.0.0.1:${local_port}"
}

local_model_store_endpoint() {
  local endpoint="$1"
  local local_port="${MODEL_STORE_LOCAL_PORT:-19000}"
  local log_dir="${JENKINS_HOME:-/tmp}/ci-tmp"
  local log_path="${log_dir}/recsys-model-store-port-forward.log"
  mkdir -p "${log_dir}"

  if [[ -z "${endpoint}" || "${endpoint}" != *".svc.cluster.local"* ]]; then
    local_model_store_endpoint_result="${endpoint}"
    return 0
  fi

  (
    exec kubectl port-forward -n "${namespace_mlops}" svc/minio "${local_port}:9000"
  ) >"${log_path}" 2>&1 9>&- &
  kfp_port_forward_pids+=("$!")
  wait_for_local_port "${local_port}" "model store endpoint" || {
    cat "${log_path}" >&2 || true
    return 1
  }
  local_model_store_endpoint_result="http://127.0.0.1:${local_port}"
}

configure_local_model_store_endpoint() {
  local endpoint="${MODEL_STORE_ENDPOINT:-${MLFLOW_S3_ENDPOINT_URL:-${MINIO_ENDPOINT:-}}}"
  local_model_store_endpoint "${endpoint}"
  endpoint="${local_model_store_endpoint_result}"
  if [[ -n "${endpoint}" ]]; then
    export MODEL_STORE_ENDPOINT="${endpoint}"
    export MLFLOW_S3_ENDPOINT_URL="${endpoint}"
    export MINIO_ENDPOINT="${endpoint}"
  fi
}

resource_exists() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  kubectl get "${kind}/${name}" -n "${namespace}" >/dev/null 2>&1
}

verify_workload_image() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  local expected_image="$4"

  if ! resource_exists "${kind}" "${name}" "${namespace}"; then
    recsys_log "skipping image check for absent ${kind}/${name} in ${namespace}"
    return 0
  fi

  local images
  images="$(kubectl get "${kind}/${name}" -n "${namespace}" -o jsonpath='{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}')"
  if [[ -n "${expected_image}" ]] && ! grep -Fq "${expected_image}" <<<"${images}"; then
    recsys_error "expected ${expected_image} on ${kind}/${name} in ${namespace}; found: ${images}"
    return 1
  fi
}

wait_rollout_if_exists() {
  local kind="$1"
  local name="$2"
  local namespace="$3"

  if ! resource_exists "${kind}" "${name}" "${namespace}"; then
    recsys_log "skipping rollout wait for absent ${kind}/${name} in ${namespace}"
    return 0
  fi
  kubectl rollout status "${kind}/${name}" -n "${namespace}" --timeout="${timeout}"
}

verify_and_wait_workload() {
  verify_workload_image "$1" "$2" "$3" "$4"
  wait_rollout_if_exists "$1" "$2" "$3"
}

load_secret_env_if_unset() {
  local namespace="$1"
  local secret_name="$2"
  shift 2

  if ! kubectl get secret "${secret_name}" -n "${namespace}" >/dev/null 2>&1; then
    recsys_log "secret ${secret_name} is absent in ${namespace}; using existing environment"
    return 0
  fi

  local key encoded value loaded=0
  for key in "$@"; do
    [[ -n "${!key:-}" ]] && continue
    encoded="$(kubectl get secret "${secret_name}" -n "${namespace}" -o "jsonpath={.data.${key}}" 2>/dev/null || true)"
    [[ -z "${encoded}" ]] && continue
    value="$(printf '%s' "${encoded}" | base64 -d)"
    export "${key}=${value}"
    loaded=1
  done
  recsys_log "model-store environment load completed for ${secret_name}; loaded=${loaded}; values hidden"
}

runtime_python() {
  if [[ -n "${UV_PROJECT_ENVIRONMENT:-}" && -x "${UV_PROJECT_ENVIRONMENT}/bin/python" ]]; then
    "${UV_PROJECT_ENVIRONMENT}/bin/python" "$@"
  else
    python3 "$@"
  fi
}
