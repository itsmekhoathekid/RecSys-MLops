#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${NAMESPACE:-api-serving}"
SERVICE="${SERVICE:-recsys-api-serving}"
LOCAL_PORT="${LOCAL_PORT:-18088}"
LOAD_TARGET="${RECSYS_LOAD_TARGET:-api}"
USERS="${LOCUST_USERS:-180}"
SPAWN_RATE="${LOCUST_SPAWN_RATE:-60}"
DURATION="${LOCUST_DURATION:-4m}"
CANDIDATE_COUNT="${RECSYS_CANDIDATE_COUNT:-200}"
TOP_K="${RECSYS_TOP_K:-10}"
USER_ID="${RECSYS_USER_ID:-4}"
PORT_FORWARD_LOG="${PORT_FORWARD_LOG:-/tmp/recsys-serving-autoscale-port-forward.log}"

cleanup() {
  if [[ -n "${PORT_FORWARD_PID:-}" ]]; then
    kill "${PORT_FORWARD_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

kubectl -n "${NAMESPACE}" port-forward "svc/${SERVICE}" "${LOCAL_PORT}:80" >"${PORT_FORWARD_LOG}" 2>&1 &
PORT_FORWARD_PID="$!"
sleep 3

echo "Initial autoscale state"
kubectl get hpa -n api-serving
kubectl get hpa -n kserve-triton-inference || true
kubectl get scaledobject -n api-serving
kubectl get deploy -n api-serving recsys-api-serving recsys-online-feature-api
kubectl get deploy -n kserve-triton-inference recsys-bst-triton-predictor || true

RECSYS_LOAD_TARGET="${LOAD_TARGET}" \
RECSYS_USER_ID="${USER_ID}" \
RECSYS_CANDIDATE_COUNT="${CANDIDATE_COUNT}" \
RECSYS_TOP_K="${TOP_K}" \
uv run --with locust locust \
  -f tests/load/locustfile_serving.py \
  --host "http://127.0.0.1:${LOCAL_PORT}" \
  --headless \
  -u "${USERS}" \
  -r "${SPAWN_RATE}" \
  -t "${DURATION}" \
  --only-summary

echo "Autoscale state after load"
kubectl get hpa -n api-serving
kubectl get hpa -n kserve-triton-inference || true
kubectl get scaledobject -n api-serving
kubectl get deploy -n api-serving recsys-api-serving recsys-online-feature-api
kubectl get deploy -n kserve-triton-inference recsys-bst-triton-predictor || true
