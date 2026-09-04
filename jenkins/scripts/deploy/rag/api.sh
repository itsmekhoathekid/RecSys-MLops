#!/usr/bin/env bash

rag_start_api_port_forward() {
  local port="${RAG_API_VERIFY_PORT:-18089}"
  local namespace="${API_NAMESPACE:-api-serving}"
  local ready_pod=""
  mkdir -p .ci-deploy

  # A Service port-forward can select a Pending pod during a rolling update.
  # Wait for the deployment, then pin the tunnel to a Running pod so promotion
  # cannot fail nondeterministically because another replica is still Pending.
  kubectl -n "${namespace}" rollout status deployment/recsys-rag-api --timeout=300s
  ready_pod="$(kubectl -n "${namespace}" get pods \
    -l app.kubernetes.io/name=recsys-rag-api \
    --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}')"
  if [[ -z "${ready_pod}" ]]; then
    echo "No Running RAG API pod is available for promotion verification" >&2
    return 1
  fi

  kubectl -n "${namespace}" port-forward "pod/${ready_pod}" "${port}:8080" >.ci-deploy/rag-api-port-forward.log 2>&1 &
  RAG_API_FORWARD_PID=$!
  if recsys_wait_http "http://127.0.0.1:${port}/ready" 30 1 \
    "${RAG_API_FORWARD_PID}"; then
    return 0
  fi
  recsys_cleanup_process "${RAG_API_FORWARD_PID}"
  return 1
}

rag_stop_api_port_forward() {
  if [[ -n "${RAG_API_FORWARD_PID:-}" ]]; then
    recsys_cleanup_process "${RAG_API_FORWARD_PID}"
    unset RAG_API_FORWARD_PID
  fi
}

rag_verify_api_contract() {
  local expected_model="${RAG_EMBEDDING_MODEL:-intfloat/multilingual-e5-small}"
  local expected_revision="${RAG_EMBEDDING_REVISION:-03415a4be176a1620747c692ed433219fabc3def}"
  local expected_dimension="${RAG_EMBEDDING_DIMENSION:-384}"
  local port="${RAG_API_VERIFY_PORT:-18089}"
  local report=".ci-deploy/rag-api-version.json"
  rag_start_api_port_forward
  if ! curl --fail --silent --show-error "http://127.0.0.1:${port}/version" >"${report}"; then
    rag_stop_api_port_forward
    return 1
  fi
  rag_stop_api_port_forward
  # Keep the promotion gate on the base Python runtime already required by the
  # Jenkins controller; production agents do not guarantee an external jq
  # binary. The report contains only public model contract metadata.
  python3 - "${report}" "${expected_model}" "${expected_revision}" "${expected_dimension}" <<'PY'
import json
import sys

report_path, expected_model, expected_revision, expected_dimension = sys.argv[1:]
with open(report_path, encoding="utf-8") as handle:
    payload = json.load(handle)

matched = any(
    contract.get("model") == expected_model
    and contract.get("revision") == expected_revision
    and contract.get("dimension") == int(expected_dimension)
    for contract in payload.get("supported_embedding_contracts", [])
)
if not matched:
    raise SystemExit("RAG API does not support the candidate embedding contract")
PY
}
