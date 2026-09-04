#!/usr/bin/env bash

# Shared bounded waits and cleanup primitives. This library intentionally does
# not modify shell options so sourcing it cannot change caller behavior.
source jenkins/scripts/lib/common.sh

recsys_cleanup_process() {
  local pid="${1:-}"
  [[ -n "${pid}" ]] || return 0
  kill "${pid}" >/dev/null 2>&1 || true
  wait "${pid}" >/dev/null 2>&1 || true
}

recsys_wait_http() {
  local url="$1"
  local attempts="${2:-30}"
  local delay_seconds="${3:-1}"
  local watched_pid="${4:-}"

  for _ in $(seq 1 "${attempts}"); do
    curl -fsS "${url}" >/dev/null 2>&1 && return 0
    if [[ -n "${watched_pid}" ]] && ! kill -0 "${watched_pid}" 2>/dev/null; then
      recsys_error "background process ${watched_pid} terminated while waiting for ${url}"
      return 1
    fi
    sleep "${delay_seconds}"
  done
  recsys_error "timed out waiting for ${url}"
  return 1
}

recsys_wait_kubernetes_job() {
  local namespace="$1"
  local job="$2"
  local timeout="${3:-1800s}"
  local visible=false

  for _ in $(seq 1 30); do
    if kubectl -n "${namespace}" get "job/${job}" >/dev/null 2>&1; then
      visible=true
      break
    fi
    sleep 1
  done
  if [[ "${visible}" != "true" ]]; then
    recsys_error "Job ${namespace}/${job} was not visible after creation"
    return 1
  fi

  local timeout_seconds="${timeout%s}"
  local deadline=$((SECONDS + timeout_seconds))
  local conditions
  while ((SECONDS < deadline)); do
    conditions="$(kubectl -n "${namespace}" get "job/${job}" -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}' 2>/dev/null || true)"
    if grep -qx 'Complete=True' <<<"${conditions}"; then
      kubectl -n "${namespace}" logs "job/${job}" --all-containers=true
      return 0
    fi
    if grep -qx 'Failed=True' <<<"${conditions}"; then
      recsys_error "Job ${namespace}/${job} reported Failed before timeout"
      kubectl -n "${namespace}" logs "job/${job}" --all-containers=true || true
      return 1
    fi
    sleep 5
  done
  recsys_error "Timed out after ${timeout} waiting for Job ${namespace}/${job}"
  kubectl -n "${namespace}" logs "job/${job}" --all-containers=true || true
  return 1
}

recsys_write_registry_evidence() {
  local path="$1"
  local version="$2"
  local commit="$3"
  shift 3
  mkdir -p "$(dirname "${path}")"
  python3 - "${path}" "${version}" "${commit}" "$@" <<'PY'
import json
import sys

path, version, commit, *artifacts = sys.argv[1:]
with open(path, "w", encoding="utf-8") as stream:
    json.dump(
        {"version": version, "git_commit": commit, "artifacts": artifacts},
        stream,
        indent=2,
        sort_keys=True,
    )
PY
}
