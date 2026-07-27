#!/usr/bin/env bash

test_analytics() {
  local namespace="${ANALYTICS_NAMESPACE:-analytics}"
  component_test_wait_deployment "${namespace}" recsys-analytics-trino
  component_test_wait_deployment "${namespace}" recsys-analytics-superset
  kubectl exec -n "${namespace}" deploy/recsys-analytics-trino -- \
    curl -fsS http://127.0.0.1:8080/v1/info >/dev/null
  kubectl exec -n "${namespace}" deploy/recsys-analytics-superset -- \
    curl -fsS http://127.0.0.1:8088/health >/dev/null
  kubectl run "recsys-dbt-smoke-${BUILD_NUMBER:-manual}" \
    -n "${namespace}" \
    --rm \
    --restart=Never \
    --attach \
    --image="$(image recsys-analytics-dbt)" \
    --command -- sh -lc '
      set -eu
      dbt parse \
        --project-dir /opt/recsys/apps/analytics \
        --profiles-dir /opt/recsys/apps/analytics/profiles
      dbt test \
        --project-dir /opt/recsys/apps/analytics \
        --profiles-dir /opt/recsys/apps/analytics/profiles
      python - <<"PY"
import json
import urllib.request

request = urllib.request.Request(
    "http://recsys-analytics-trino:8080/v1/statement",
    data=b"SELECT count(*) FROM analytics.recsys.mart_recsys_funnel_daily",
    headers={"X-Trino-User": "ci-transaction"},
)
payload = json.load(urllib.request.urlopen(request, timeout=30))
while payload.get("nextUri"):
    payload = json.load(urllib.request.urlopen(payload["nextUri"], timeout=30))
if payload.get("error"):
    raise SystemExit(payload["error"])
assert payload.get("data"), payload
PY
    '
}
