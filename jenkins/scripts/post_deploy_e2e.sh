#!/usr/bin/env bash
set -euo pipefail

api_namespace="${API_NAMESPACE:-api-serving}"
kserve_namespace="${KSERVE_NAMESPACE:-kserve-triton-inference}"
data_namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
fastapi_service="${FASTAPI_SERVICE:-recsys-api-serving}"
fastapi_port="${FASTAPI_PORT:-8088}"
run_feature_store_verify="${RUN_FEATURE_STORE_VERIFY:-1}"
run_observability_smoke="${RUN_OBSERVABILITY_SMOKE:-1}"

port_forward_pid=""

cleanup() {
  if [[ -n "${port_forward_pid}" ]]; then
    kill "${port_forward_pid}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

wait_for_port() {
  local port="$1"
  local label="$2"
  for _ in $(seq 1 60); do
    if (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${label} on 127.0.0.1:${port}" >&2
  return 1
}

kubectl wait --for=condition=Ready "inferenceservice/recsys-bst-triton" \
  -n "${kserve_namespace}" --timeout=600s
kubectl rollout status "deployment/${fastapi_service}" \
  -n "${api_namespace}" --timeout=300s

kubectl port-forward -n "${api_namespace}" "svc/${fastapi_service}" "${fastapi_port}:80" \
  >/tmp/recsys-post-deploy-fastapi-port-forward.log 2>&1 &
port_forward_pid="$!"
wait_for_port "${fastapi_port}" "FastAPI"

curl -fsS "http://127.0.0.1:${fastapi_port}/healthz" >/dev/null
curl -fsS "http://127.0.0.1:${fastapi_port}/ready" >/dev/null
curl -fsS "http://127.0.0.1:${fastapi_port}/version" | python3 -m json.tool
curl -fsS -X POST "http://127.0.0.1:${fastapi_port}/recommendations" \
  -H 'Content-Type: application/json' \
  -d '{"user_id":1,"candidate_item_ids":[1,2,3,4,5],"top_k":3}' | python3 -m json.tool
curl -fsS "http://127.0.0.1:${fastapi_port}/metrics" \
  | rg "model_predictions_total|recsys_api_triton_inference_duration_seconds" >/dev/null

RECSYS_LIVE_E2E=1 uv run pytest tests/e2e/test_live_serving_flow.py -q

if [[ "${run_feature_store_verify}" == "1" ]]; then
  DATA_PLATFORM_NAMESPACE="${data_namespace}" infra/k8s/scripts/data_platform_verify_feature_stores.sh
fi

if [[ "${run_observability_smoke}" == "1" ]]; then
  make observability-smoke
fi

echo "Post-deploy E2E validation passed."
