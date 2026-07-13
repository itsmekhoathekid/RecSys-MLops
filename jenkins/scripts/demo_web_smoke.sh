#!/usr/bin/env bash
set -euo pipefail

namespace="${DEMO_WEB_NAMESPACE:-api-serving}"
backend_url="${DEMO_WEB_BACKEND_URL:-http://recsys-demo-api.${namespace}.svc.cluster.local}"
frontend_url="${DEMO_WEB_FRONTEND_URL:-http://recsys-demo-web.${namespace}.svc.cluster.local}"
public_url="${DEMO_WEB_PUBLIC_URL:-https://recsys-mlops.site}"
poll_timeout="${DEMO_WEB_FEATURE_POLL_TIMEOUT_SECONDS:-60}"
mkdir -p .demo-web

# Jenkins is intentionally outside the api-serving Istio mesh. Execute internal
# checks from the backend workload so service calls use its mTLS sidecar without
# granting the CI namespace direct access to production services.
mesh_request() {
  local method="$1"
  local url="$2"
  local body="${3:-}"
  local idempotency_key="${4:-}"

  kubectl exec -n "${namespace}" deploy/recsys-demo-api -c backend -- \
    /opt/venv/bin/python -c '
import sys

import httpx

method, url, body, idempotency_key = sys.argv[1:]
headers = {}
if body:
    headers["Content-Type"] = "application/json"
if idempotency_key:
    headers["Idempotency-Key"] = idempotency_key
response = httpx.request(method, url, content=body or None, headers=headers, timeout=15.0)
response.raise_for_status()
sys.stdout.write(response.text)
' "${method}" "${url}" "${body}" "${idempotency_key}"
}

mesh_request GET "${frontend_url}/nginx-health" >/dev/null
mesh_request GET "${backend_url}/healthz" >/dev/null
mesh_request GET "${backend_url}/ready" >/dev/null

http_redirect="$(curl -sS -o /dev/null -w '%{http_code}' "http://recsys-mlops.site/" || true)"
if [[ "${http_redirect}" != "301" && "${http_redirect}" != "308" ]]; then
  echo "Expected HTTP redirect from recsys-mlops.site, got ${http_redirect}" >&2
  exit 1
fi
unauthenticated="$(curl -sS -o /dev/null -w '%{http_code}' "${public_url}/" || true)"
if [[ "${unauthenticated}" != "401" ]]; then
  echo "Expected Basic Auth 401 from ${public_url}, got ${unauthenticated}" >&2
  exit 1
fi

if [[ -n "${GATEWAY_SMOKE_USER:-}" && -n "${GATEWAY_SMOKE_PASSWORD:-}" ]]; then
  curl -fsS -u "${GATEWAY_SMOKE_USER}:${GATEWAY_SMOKE_PASSWORD}" "${public_url}/" \
    | grep -Fq '<div id="root"></div>'
fi

users_json="$(mesh_request GET "${backend_url}/api/users?limit=1&offset=0")"
products_json="$(mesh_request GET "${backend_url}/api/products?limit=1&offset=0")"
user_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["user_id"])' <<<"${users_json}")"
product_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["items"][0]["product_id"])' <<<"${products_json}")"
session_id="smoke-session-$(date +%s)"
idempotency_key="smoke-$(date +%s)-${BUILD_NUMBER:-local}"

event_json="$(mesh_request POST "${backend_url}/api/events" \
  "{\"user_id\":${user_id},\"product_id\":${product_id},\"action\":\"view\",\"session_id\":\"${session_id}\"}" \
  "${idempotency_key}")"
event_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["event_id"])' <<<"${event_json}")"

deadline=$((SECONDS + poll_timeout))
feature_status="accepted"
while (( SECONDS < deadline )); do
  status_json="$(mesh_request GET "${backend_url}/api/events/${event_id}/status")"
  feature_status="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"${status_json}")"
  if [[ "${feature_status}" == "feature_store_updated" ]]; then
    break
  fi
  sleep 2
done
if [[ "${feature_status}" != "feature_store_updated" ]]; then
  echo "Event ${event_id} did not reach the online feature store within ${poll_timeout}s" >&2
  exit 1
fi

mesh_request POST "${backend_url}/api/recommendations" \
  "{\"user_id\":${user_id},\"session_id\":\"${session_id}\",\"top_k\":3}" \
  | tee .demo-web/recommendation-smoke.json \
  | python3 -c 'import json,sys; body=json.load(sys.stdin); assert body["model_version"]; assert body["items"]'

echo "Demo web production smoke passed."
