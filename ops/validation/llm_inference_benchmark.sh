#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${LLM_NAMESPACE:-llm-inference}"
GATEWAY="${LLM_GATEWAY:-llm-d-inference-gateway}"
MODEL="${LLM_MODEL:-qwen3.5-0.8b}"
SPEC="${LLMDBENCH_SPEC:-guides/optimized-baseline}"
WORKLOAD="${LLMDBENCH_WORKLOAD:-sanity_random.yaml}"
HARNESS="${LLMDBENCH_HARNESS:-inference-perf}"
WORKSPACE="${LLMDBENCH_WORKSPACE:-reports/llm-d/$(date -u +%Y%m%dT%H%M%SZ)}"
ENDPOINT_URL="${LLMDBENCH_ENDPOINT_URL:-}"

for tool in kubectl llmdbenchmark; do
  command -v "${tool}" >/dev/null 2>&1 || {
    echo "Missing required command: ${tool}" >&2
    exit 1
  }
done

if [[ -z "${ENDPOINT_URL}" ]]; then
  address="$(kubectl get gateway "${GATEWAY}" -n "${NAMESPACE}" \
    -o jsonpath='{.status.addresses[0].value}' 2>/dev/null || true)"
  if [[ -n "${address}" ]]; then
    ENDPOINT_URL="http://${address}:80"
  fi
fi

if [[ -z "${ENDPOINT_URL}" ]]; then
  echo "Gateway has no address yet. Export LLMDBENCH_ENDPOINT_URL=http://HOST:PORT and retry." >&2
  exit 1
fi

mkdir -p "${WORKSPACE}"
echo "Benchmark endpoint: ${ENDPOINT_URL}"
echo "Results workspace: ${WORKSPACE}"

llmdbenchmark \
  --spec "${SPEC}" \
  --workspace "${WORKSPACE}" \
  run \
  --endpoint-url "${ENDPOINT_URL}" \
  --model "${MODEL}" \
  --namespace "${NAMESPACE}" \
  --harness "${HARNESS}" \
  --workload "${WORKLOAD}" \
  --monitoring \
  --analyze
