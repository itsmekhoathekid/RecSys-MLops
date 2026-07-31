#!/usr/bin/env bash
set -euo pipefail

namespace="${DEMO_WEB_NAMESPACE:-api-serving}"
backend_url="${DEMO_WEB_BACKEND_URL:-http://recsys-demo-api.${namespace}.svc.cluster.local}"
frontend_url="${DEMO_WEB_FRONTEND_URL:-http://recsys-demo-web.${namespace}.svc.cluster.local}"
public_url="${DEMO_WEB_PUBLIC_URL:-https://recsys-mlops.site}"

# Jenkins is intentionally outside the api-serving Istio mesh. Execute internal
# checks from the backend workload so service calls use its mTLS sidecar without
# granting the CI namespace direct access to production services.
mesh_request() {
  local url="$1"

  kubectl exec -n "${namespace}" deploy/recsys-demo-api -c backend -- \
    /opt/venv/bin/python -c '
import sys

import httpx

response = httpx.get(sys.argv[1], timeout=15.0)
response.raise_for_status()
sys.stdout.write(response.text)
' "${url}"
}

mesh_request "${frontend_url}/nginx-health" >/dev/null
mesh_request "${backend_url}/healthz" >/dev/null
mesh_request "${backend_url}/ready" >/dev/null

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

echo "Demo web deployment health verification passed."
