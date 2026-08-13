#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${LLM_NAMESPACE:-llm-inference}"
DEPLOYMENT="${LLM_DEPLOYMENT:-qwen35-gguf}"
SERVICE="${LLM_SERVICE:-qwen35-gguf}"
GATEWAY="${LLM_GATEWAY:-llm-d-inference-gateway}"
GATEWAY_ROUTE_MODEL="${LLM_GATEWAY_ROUTE_MODEL:-llm-d-optimized-baseline}"
MODEL="${LLM_MODEL:-qwen3.5-0.8b}"
MAX_TOKENS="${LLM_SMOKE_MAX_TOKENS:-4}"
LOCAL_PORT="${LLM_SMOKE_PORT:-18000}"
TIMEOUT="${LLM_SMOKE_TIMEOUT:-20m}"
PORT_FORWARD_PID=""

cleanup() {
  if [[ -n "${PORT_FORWARD_PID}" ]]; then
    kill "${PORT_FORWARD_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

for tool in kubectl curl; do
  command -v "${tool}" >/dev/null 2>&1 || {
    echo "Missing required command: ${tool}" >&2
    exit 1
  }
done

echo "Waiting for ${NAMESPACE}/${DEPLOYMENT}..."
kubectl rollout status "deployment/${DEPLOYMENT}" -n "${NAMESPACE}" --timeout="${TIMEOUT}"

echo "Checking agentgateway and llm-d route resources..."
kubectl wait "gateway.gateway.networking.k8s.io/${GATEWAY}" -n "${NAMESPACE}" \
  --for=condition=Programmed --timeout="${TIMEOUT}"
kubectl get gateways.gateway.networking.k8s.io,httproutes.gateway.networking.k8s.io,inferencepools.inference.networking.k8s.io \
  -n "${NAMESPACE}"

kubectl port-forward -n "${NAMESPACE}" "service/${SERVICE}" \
  "${LOCAL_PORT}:8000" >/tmp/recsys-llm-port-forward.log 2>&1 &
PORT_FORWARD_PID="$!"

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${LOCAL_PORT}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

curl -fsS "http://127.0.0.1:${LOCAL_PORT}/health"
echo
curl -fsS "http://127.0.0.1:${LOCAL_PORT}/v1/models"
echo
curl -fsS "http://127.0.0.1:${LOCAL_PORT}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: ready\"}],\"max_tokens\":${MAX_TOKENS},\"temperature\":0}"
echo

GATEWAY_ADDRESS="$(kubectl get gateway.gateway.networking.k8s.io "${GATEWAY}" \
  -n "${NAMESPACE}" -o jsonpath='{.status.addresses[0].value}')"
if [[ -z "${GATEWAY_ADDRESS}" ]]; then
  echo "Gateway is Programmed but has no address." >&2
  exit 1
fi

curl -fsS "http://${GATEWAY_ADDRESS}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -H "X-Gateway-Base-Model-Name: ${GATEWAY_ROUTE_MODEL}" \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: gateway-ready\"}],\"max_tokens\":${MAX_TOKENS},\"temperature\":0}"
echo
echo "LLM inference smoke test passed."
