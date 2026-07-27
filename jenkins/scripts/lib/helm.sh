#!/usr/bin/env bash

helm_atomic_upgrade() {
  local release="$1"
  local chart="$2"
  local namespace="$3"
  local timeout="$4"
  shift 4
  helm upgrade --install "${release}" "${chart}" \
    --namespace "${namespace}" \
    --create-namespace \
    --atomic \
    --cleanup-on-fail \
    --wait \
    --wait-for-jobs \
    --history-max "${HELM_HISTORY_MAX:-10}" \
    --timeout "${timeout}" \
    "$@"
}

helm_current_revision() {
  local release="$1"
  local namespace="$2"
  helm history "${release}" -n "${namespace}" -o json 2>/dev/null \
    | python3 -c '
import json, sys
rows = json.load(sys.stdin)
deployed = [row for row in rows if str(row.get("status", "")).lower() == "deployed"]
print(deployed[-1]["revision"] if deployed else "")
' 2>/dev/null
}
