#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"
PIPELINE_IMAGE="${RECSYS_PIPELINE_IMAGE:-recsys-mlops-training:local}"
KFP_PORT="${RECSYS_KFP_PORT:-8888}"
MINIO_PORT="${RECSYS_MINIO_PORT:-9000}"
FASTAPI_PORT="${RECSYS_FASTAPI_PORT:-8088}"
GRAFANA_PORT="${RECSYS_GRAFANA_PORT:-3000}"
PROMETHEUS_PORT="${RECSYS_PROMETHEUS_PORT:-9090}"
PIPELINE_PACKAGE="${RECSYS_KFP_PACKAGE_PATH:-infra/kubeflow/compiled/bst_training_pipeline.yaml}"
PIPELINE_EXPERIMENT="${RECSYS_KFP_EXPERIMENT_NAME:-recsys-bst-ranking}"
PIPELINE_RUN_NAME="${RECSYS_KFP_RUN_NAME:-recsys-bst-full-flow-$(date -u +%Y%m%d%H%M%S)}"
PIPELINE_TIMEOUT_SECONDS="${RECSYS_KFP_TIMEOUT_SECONDS:-7200}"
PIPELINE_POLL_SECONDS="${RECSYS_KFP_POLL_SECONDS:-30}"
PROMOTION_MANIFEST_PATH="${RECSYS_PROMOTION_MANIFEST_PATH:-/workspace/recsys/data_platform/output/ml/serving/promotion_manifest.json}"
LATEST_MANIFEST_URI="${RECSYS_LATEST_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/latest.json}"
RUN_DATA_SETUP="${RECSYS_E2E_RUN_DATA_SETUP:-0}"
REQUEST_COUNT="${RECSYS_E2E_REQUEST_COUNT:-40}"
SKIP_CLUSTER_UP="${RECSYS_E2E_SKIP_CLUSTER_UP:-0}"

PORT_FORWARD_PIDS=()

section() {
  printf "\n== %s ==\n" "$1"
}

run_make() {
  make -C "${ROOT_DIR}" "$@"
}

load_secret_env_if_unset() {
  local namespace="$1"
  local secret_name="$2"
  shift 2

  local key
  local value
  for key in "$@"; do
    if [[ -n "${!key:-}" ]]; then
      continue
    fi
    value="$(
      kubectl get secret "${secret_name}" -n "${namespace}" \
        -o "jsonpath={.data.${key}}" 2>/dev/null | base64 --decode || true
    )"
    if [[ -n "${value}" ]]; then
      export "${key}=${value}"
    fi
  done
}

cleanup() {
  for pid in "${PORT_FORWARD_PIDS[@]:-}"; do
    kill "${pid}" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

wait_for_port() {
  local port="$1"
  local name="$2"
  for _ in $(seq 1 90); do
    if (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${name} on 127.0.0.1:${port}"
  return 1
}

start_port_forward() {
  local namespace="$1"
  local service="$2"
  local local_port="$3"
  local remote_port="$4"
  local name="$5"
  local log_path="/tmp/recsys-${name}-port-forward.log"
  kubectl port-forward -n "${namespace}" "svc/${service}" "${local_port}:${remote_port}" >"${log_path}" 2>&1 &
  PORT_FORWARD_PIDS+=("$!")
  wait_for_port "${local_port}" "${name}" || {
    cat "${log_path}" || true
    return 1
  }
}

read_promotion_manifest() {
  local pod_name="recsys-read-promotion-manifest-$(date -u +%s)"
  local overrides
  overrides="$(printf '{"metadata":{"annotations":{"sidecar.istio.io/inject":"false"}},"spec":{"securityContext":{"seccompProfile":{"type":"RuntimeDefault"}},"volumes":[{"name":"workspace","persistentVolumeClaim":{"claimName":"recsys-mlops-pvc"}}],"containers":[{"name":"%s","image":"%s","imagePullPolicy":"IfNotPresent","command":["sh","-lc","cat %s"],"securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}},"volumeMounts":[{"name":"workspace","mountPath":"/workspace"}]}]}}' "${pod_name}" "${PIPELINE_IMAGE}" "${PROMOTION_MANIFEST_PATH}")"
  kubectl run "${pod_name}" \
    -n kubeflow \
    --rm \
    -i \
    --quiet \
    --restart=Never \
    --image="${PIPELINE_IMAGE}" \
    --image-pull-policy=IfNotPresent \
    --overrides="${overrides}" \
    | python3 -c 'import sys; text=sys.stdin.read(); start=text.find("{"); end=text.rfind("}"); print(text[start:end + 1] if start >= 0 and end >= start else text)'
}

json_field() {
  local field="$1"
  python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "${field}"
}

submit_pipeline() {
  uv run python apps/ml-system/src/kubeflow/submit_pipeline_run.py \
    --host "http://127.0.0.1:${KFP_PORT}" \
    --package-path "${PIPELINE_PACKAGE}" \
    --experiment-name "${PIPELINE_EXPERIMENT}" \
    --run-name "${PIPELINE_RUN_NAME}" \
    --timeout-seconds "${PIPELINE_TIMEOUT_SECONDS}" \
    --poll-seconds "${PIPELINE_POLL_SECONDS}"
}

promote_and_deploy() {
  local candidate_manifest_uri="$1"
  load_secret_env_if_unset kubeflow "${MLOPS_RUNTIME_SECRET_NAME:-recsys-mlops-runtime}" \
    MINIO_ROOT_USER \
    MINIO_ROOT_PASSWORD \
    AWS_ACCESS_KEY_ID \
    AWS_SECRET_ACCESS_KEY \
    AWS_DEFAULT_REGION
  export MINIO_ENDPOINT="http://127.0.0.1:${MINIO_PORT}"
  export MLFLOW_S3_ENDPOINT_URL="${MINIO_ENDPOINT}"
  export MINIO_ROOT_USER="${MINIO_ROOT_USER:-minio}"
  export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minio123}"
  export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-${MINIO_ROOT_USER}}"
  export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-${MINIO_ROOT_PASSWORD}}"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
  export RECSYS_MODEL_CD_ATOMIC="${RECSYS_MODEL_CD_ATOMIC:-0}"

  if uv run python jenkins/scripts/model_cd.py \
    --stage promote \
    --manifest-uri "${LATEST_MANIFEST_URI}" \
    --control-manifest-uri "${LATEST_MANIFEST_URI}" \
    --candidate-manifest-uri "${candidate_manifest_uri}" \
    --apply \
    --timeout 600s; then
    return 0
  fi

  echo "No usable latest control manifest yet; bootstrapping latest from candidate."
  uv run python jenkins/scripts/model_cd.py \
    --stage promote \
    --manifest-uri "${LATEST_MANIFEST_URI}" \
    --control-manifest-uri "${candidate_manifest_uri}" \
    --candidate-manifest-uri "${candidate_manifest_uri}" \
    --apply \
    --timeout 600s
}

generate_serving_traffic() {
  local first_response=""
  for i in $(seq 1 "${REQUEST_COUNT}"); do
    local user_id=$((1000 + i))
    local body
    body="$(printf '{"user_id":%d,"candidate_item_ids":[1,2,3,4,5,6,7,8,9,10],"top_k":5}' "${user_id}")"
    local response
    response=""
    for _ in $(seq 1 12); do
      if response="$(curl -fsS -X POST "http://127.0.0.1:${FASTAPI_PORT}/recommendations" \
        -H 'Content-Type: application/json' \
        -d "${body}")"; then
        break
      fi
      sleep 5
    done
    if [[ -z "${response}" ]]; then
      echo "Recommendation API did not return a successful response for user ${user_id}"
      return 1
    fi
    if [[ -z "${first_response}" ]]; then
      first_response="${response}"
    fi
  done
  echo "${first_response}" | python3 -m json.tool
}

wait_for_prometheus_metric() {
  local query="$1"
  for _ in $(seq 1 60); do
    if curl -fsS -G "http://127.0.0.1:${PROMETHEUS_PORT}/api/v1/query" --data-urlencode "query=${query}" \
      | python3 -c 'import json,sys; data=json.load(sys.stdin)["data"]["result"]; sys.exit(0 if data and float(data[0]["value"][1]) > 0 else 1)' >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  echo "Prometheus metric did not become positive: ${query}"
  return 1
}

verify_grafana_dashboard() {
  curl -fsS -u admin:admin "http://127.0.0.1:${GRAFANA_PORT}/api/health" >/dev/null
  curl -fsS -u admin:admin "http://127.0.0.1:${GRAFANA_PORT}/api/search?query=A%2FB" \
    | python3 -c 'import json,sys; dashboards=json.load(sys.stdin); sys.exit(0 if any("A/B" in item.get("title","") for item in dashboards) else 1)'
}

if [[ "${SKIP_CLUSTER_UP}" == "1" ]]; then
  section "Use Existing Full Service Cluster"
  kubectl config use-context "${KUBE_CONTEXT:-${PROFILE}}" >/dev/null || true
else
  section "Start Full Service Cluster"
  MINIKUBE_PROFILE="${PROFILE}" run_make cluster-up
fi

if [[ "${RUN_DATA_SETUP}" == "1" ]]; then
  section "Run Data Setup First"
  MINIKUBE_PROFILE="${PROFILE}" run_make cluster-data-setup
fi

section "Compile Kubeflow Pipeline"
run_make mlops-compile-kfp

section "Submit Kubeflow Pipeline And Wait"
start_port_forward kubeflow ml-pipeline "${KFP_PORT}" 8888 kfp
submit_pipeline

section "Read Candidate Promotion Manifest"
promotion_manifest_json="$(read_promotion_manifest)"
echo "${promotion_manifest_json}" | python3 -m json.tool
candidate_manifest_uri="$(printf '%s' "${promotion_manifest_json}" | json_field promotion_manifest_uri)"
echo "Candidate manifest URI: ${candidate_manifest_uri}"

section "Promote Through Model CD And Deploy Triton/FastAPI"
start_port_forward experiment-tracking minio "${MINIO_PORT}" 9000 minio
promote_and_deploy "${candidate_manifest_uri}"
kubectl wait --for=condition=Available deployment/recsys-api-serving -n api-serving --timeout=240s
kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton -n kserve-triton-inference --timeout=600s

section "Call FastAPI Recommendations Against Triton"
start_port_forward api-serving recsys-api-serving "${FASTAPI_PORT}" 80 fastapi
curl -fsS "http://127.0.0.1:${FASTAPI_PORT}/healthz" >/dev/null
generate_serving_traffic
curl -fsS "http://127.0.0.1:${FASTAPI_PORT}/metrics" | rg "model_predictions_total|recsys_api_triton_inference_duration_seconds" >/dev/null

section "Verify Grafana Dashboard And Prometheus Metrics"
start_port_forward observability recsys-grafana "${GRAFANA_PORT}" 3000 grafana
start_port_forward observability recsys-prometheus "${PROMETHEUS_PORT}" 9090 prometheus
verify_grafana_dashboard
wait_for_prometheus_metric "sum(model_predictions_total)"
wait_for_prometheus_metric "sum(recsys_api_triton_inference_duration_seconds_count)"

section "Full MLOps Serving E2E Complete"
echo "Kubeflow run: ${PIPELINE_RUN_NAME}"
echo "Candidate manifest: ${candidate_manifest_uri}"
