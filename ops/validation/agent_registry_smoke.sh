#!/usr/bin/env bash
set -Eeuo pipefail

namespace="${AGENT_REGISTRY_NAMESPACE:-agentregistry}"
local_port="${AGENT_REGISTRY_LOCAL_PORT:-12121}"
port_forward_log="$(mktemp "${TMPDIR:-/tmp}/agentregistry-port-forward.XXXXXX")"
port_forward_pid=""

cleanup() {
  if [[ -n "${port_forward_pid}" ]]; then
    kill "${port_forward_pid}" >/dev/null 2>&1 || true
    wait "${port_forward_pid}" >/dev/null 2>&1 || true
  fi
  rm -f "${port_forward_log}"
}
trap cleanup EXIT

echo "== Helm releases =="
helm list -n "${namespace}"

echo "== Kubernetes resources =="
kubectl rollout status deployment/agentregistry \
  -n "${namespace}" --timeout=300s
kubectl rollout status statefulset/agentregistry-postgres \
  -n "${namespace}" --timeout=300s
kubectl get pods,service,pvc -n "${namespace}" -o wide
kubectl get externalsecret agentregistry-runtime -n "${namespace}"

echo "== pgvector extension =="
kubectl exec -n "${namespace}" agentregistry-postgres-0 -- \
  sh -ec 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT extversion FROM pg_extension WHERE extname = '\''vector'\'';"'

echo "== Agent Registry UI and OpenAPI =="
kubectl port-forward -n "${namespace}" service/agentregistry \
  "${local_port}:12121" >"${port_forward_log}" 2>&1 &
port_forward_pid="$!"

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${local_port}/openapi.json" \
    -o /dev/null 2>/dev/null; then
    break
  fi
  if ! kill -0 "${port_forward_pid}" >/dev/null 2>&1; then
    sed -n '1,120p' "${port_forward_log}" >&2
    exit 1
  fi
  sleep 1
done

ui_status="$(curl -fsS -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:${local_port}/")"
openapi_status="$(curl -fsS -o /dev/null -w '%{http_code}' \
  "http://127.0.0.1:${local_port}/openapi.json")"

test "${ui_status}" = "200"
test "${openapi_status}" = "200"
printf 'UI HTTP %s\nOpenAPI HTTP %s\n' "${ui_status}" "${openapi_status}"
