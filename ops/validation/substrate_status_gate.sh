#!/usr/bin/env bash
set -Eeuo pipefail

namespace="kagent"
controller_service="kagent-controller"
controller_local_port="18083"
status_url=""
output=""
port_forward_pid=""
port_forward_log=""
temporary_output=""

cleanup() {
  if [[ -n "${port_forward_pid}" ]]; then
    kill "${port_forward_pid}" >/dev/null 2>&1 || true
    wait "${port_forward_pid}" >/dev/null 2>&1 || true
  fi
  [[ -z "${port_forward_log}" ]] || rm -f -- "${port_forward_log}"
  [[ -z "${temporary_output}" ]] || rm -f -- "${temporary_output}"
}
trap cleanup EXIT

while (($# > 0)); do
  case "$1" in
    --namespace) namespace="${2:?namespace is required}"; shift 2 ;;
    --controller-service) controller_service="${2:?service is required}"; shift 2 ;;
    --controller-local-port) controller_local_port="${2:?port is required}"; shift 2 ;;
    --status-url) status_url="${2:?URL is required}"; shift 2 ;;
    --output) output="${2:?output is required}"; shift 2 ;;
    *) printf 'unsupported argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ -n "${output}" ]] || {
  printf -- '--output is required\n' >&2
  exit 2
}
command -v curl >/dev/null
command -v python3 >/dev/null

if [[ -z "${status_url}" ]]; then
  command -v kubectl >/dev/null
  port_forward_log="$(mktemp "${TMPDIR:-/tmp}/substrate-status-port-forward.XXXXXX")"
  kubectl -n "${namespace}" port-forward "service/${controller_service}" \
    "${controller_local_port}:8083" >"${port_forward_log}" 2>&1 &
  port_forward_pid=$!
  status_url="http://127.0.0.1:${controller_local_port}/api/substrate/status"
fi

mkdir -p "$(dirname "${output}")"
temporary_output="${output}.tmp"
ready=false
for ((attempt = 0; attempt < 30; attempt++)); do
  if curl -fsS --max-time 30 \
    "${status_url}?namespace=${namespace}" -o "${temporary_output}"; then
    ready=true
    break
  fi
  if [[ -n "${port_forward_pid}" ]] \
    && ! kill -0 "${port_forward_pid}" >/dev/null 2>&1; then
    sed -n '1,80p' "${port_forward_log}" >&2
    printf 'kagent controller port-forward exited early\n' >&2
    exit 2
  fi
  sleep 1
done
[[ "${ready}" == true ]] || {
  printf 'timed out querying the Substrate status API\n' >&2
  exit 2
}

python3 - "${temporary_output}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
data = payload.get("data") or {}
if payload.get("error") is not False:
    raise SystemExit("Substrate status API returned an error")
if data.get("enabled") is not True:
    raise SystemExit("Substrate is not enabled")
if data.get("ateApiError"):
    raise SystemExit(f"ATE API is unhealthy: {data['ateApiError']}")
if not isinstance(data.get("actors"), list):
    raise SystemExit("Substrate actor inventory is incomplete")
PY
mv -- "${temporary_output}" "${output}"
temporary_output=""
printf 'validated complete Substrate actor inventory\n'
