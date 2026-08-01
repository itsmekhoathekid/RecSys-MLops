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
