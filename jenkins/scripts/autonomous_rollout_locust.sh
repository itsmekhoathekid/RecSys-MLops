#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${repo_root}"

legacy_duration=""
if [[ "${1:-}" =~ ^[0-9]+[smhd]$ ]]; then
  legacy_duration="${1}"
  users="${2:-10}"
  spawn_rate="${3:-2}"
  max_duration="${ROLLOUT_MAX_DURATION:-45m}"
else
  users="${1:-10}"
  spawn_rate="${2:-2}"
  max_duration="${3:-${ROLLOUT_MAX_DURATION:-45m}}"
fi

if [[ ! "${users}" =~ ^[0-9]+$ ]] || (( users < 1 )); then
  echo "Locust users must be a positive integer, got: ${users}" >&2
  exit 2
fi
if [[ ! "${spawn_rate}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "Locust spawn rate must be numeric, got: ${spawn_rate}" >&2
  exit 2
fi

if [[ "${ROLLOUT_LOAD_PRINT_CONFIG:-0}" == "1" ]]; then
  echo "users=${users} spawn_rate=${spawn_rate} max_duration=${max_duration} legacy_duration=${legacy_duration:-none}"
  exit 0
fi
registry_version="${REGISTRY_VERSION:-}"
local_port="${RECSYS_LOCUST_PORT:-18088}"
reports_dir="${REPORTS_DIR:-reports}/autonomous-rollout"
port_forward_log="${TMPDIR:-/tmp}/recsys-autonomous-rollout-port-forward.log"
watcher_namespace="${ROLLOUT_WATCHER_NAMESPACE:-ci}"
watcher_deployment="${ROLLOUT_WATCHER_DEPLOYMENT:-recsys-model-rollout-watcher}"
watcher_container="${ROLLOUT_WATCHER_CONTAINER:-watcher}"
controller="/opt/recsys/apps/ml-system/src/cli/model_rollout_controller.py"
locust_pid=""

mkdir -p "${reports_dir}"

kubectl port-forward -n api-serving service/recsys-api-serving "${local_port}:80" \
  >"${port_forward_log}" 2>&1 &
port_forward_pid=$!

stop_locust() {
  [[ -n "${locust_pid}" ]] || return 0
  kill -TERM "${locust_pid}" 2>/dev/null || true
  for _ in $(seq 1 10); do
    if ! kill -0 "${locust_pid}" 2>/dev/null; then
      wait "${locust_pid}" 2>/dev/null || true
      locust_pid=""
      return 0
    fi
    sleep 1
  done
  kill -KILL "${locust_pid}" 2>/dev/null || true
  wait "${locust_pid}" 2>/dev/null || true
  locust_pid=""
}

cleanup() {
  stop_locust
  kill "${port_forward_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${local_port}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:${local_port}/healthz" >/dev/null

export RECSYS_LOAD_TARGET=api
export RECSYS_USER_ID_START="${RECSYS_USER_ID_START:-1}"
export RECSYS_USER_ID_RANGE="${RECSYS_USER_ID_RANGE:-1000000}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-${repo_root}/.uv-cache}"

if [[ -z "${registry_version}" ]]; then
  registry_version="$(
    kubectl exec -n "${watcher_namespace}" "deployment/${watcher_deployment}" \
      -c "${watcher_container}" -- python -c '
import os
from mlflow.tracking import MlflowClient

client = MlflowClient(tracking_uri=os.environ["MLFLOW_TRACKING_URI"])
name = os.environ["MLFLOW_REGISTERED_MODEL_NAME"]
versions = client.search_model_versions(filter_string=f"name=\x27{name}\x27", max_results=100)
active = [
    version for version in versions
    if version.tags.get("candidate") in {"test", "testing", "tested"}
]
if not active:
    raise SystemExit("No active MLflow candidate found")
print(max(active, key=lambda version: int(version.version)).version)
'
  )"
fi

rollout_status() {
  kubectl exec -n "${watcher_namespace}" "deployment/${watcher_deployment}" \
    -c "${watcher_container}" -- \
    python "${controller}" status --version "${registry_version}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["rollout_status"])'
}

echo "Locust is generating real sticky A/B traffic; the watcher owns 10% -> 25% -> 50% -> champion."
if [[ -n "${legacy_duration}" ]]; then
  echo "Legacy duration ${legacy_duration} detected; traffic now runs until a terminal rollout state (safety timeout ${max_duration})."
fi
echo "MLflow registry version: ${registry_version}; safety timeout: ${max_duration}."
locust_bin="${LOCUST_BIN:-$(uv run python -c 'import shutil; print(shutil.which("locust") or "")')}"
if [[ -z "${locust_bin}" || ! -x "${locust_bin}" ]]; then
  echo "Locust executable was not found in the project environment." >&2
  exit 2
fi
"${locust_bin}" \
  -f tests/load/locustfile_serving.py \
  --headless \
  --host "http://127.0.0.1:${local_port}" \
  --users "${users}" \
  --spawn-rate "${spawn_rate}" \
  --run-time "${max_duration}" \
  --html "${reports_dir}/locust-autonomous-rollout.html" \
  --csv "${reports_dir}/locust-autonomous-rollout" \
  --only-summary &
locust_pid=$!

terminal_status=""
while kill -0 "${locust_pid}" 2>/dev/null; do
  status="$(rollout_status || true)"
  [[ -n "${status}" ]] && echo "rollout_status=${status}"
  case "${status}" in
    champion|rolled_back|shadow_failed)
      terminal_status="${status}"
      stop_locust
      break
      ;;
  esac
  sleep 20
done

if [[ -n "${locust_pid}" ]]; then
  wait "${locust_pid}" 2>/dev/null || true
  locust_pid=""
fi

echo "Locust report: ${reports_dir}/locust-autonomous-rollout.html"
if [[ -z "${terminal_status}" ]]; then
  echo "Rollout did not reach a terminal state before ${max_duration}." >&2
  exit 3
fi
echo "Autonomous rollout reached terminal state: ${terminal_status}."
[[ "${terminal_status}" == "champion" ]]
