#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."

namespace="${COORDINATOR_NAMESPACE:-kagent}"
worker_pool="recsys-coordinator-sandbox-pool"
deployment="${worker_pool}-deployment"
scaled_object="${worker_pool}"
hpa="keda-hpa-${worker_pool}"
local_port="${COORDINATOR_AUTOSCALE_LOCAL_PORT:-18087}"
request_count="${COORDINATOR_AUTOSCALE_REQUESTS:-20}"
concurrency="${COORDINATOR_AUTOSCALE_CONCURRENCY:-8}"
request_timeout="${COORDINATOR_AUTOSCALE_REQUEST_TIMEOUT_SECONDS:-240}"
scale_out_timeout="${COORDINATOR_SCALE_OUT_TIMEOUT_SECONDS:-240}"
scale_down_timeout="${COORDINATOR_SCALE_DOWN_TIMEOUT_SECONDS:-420}"
fallback_timeout="${COORDINATOR_FALLBACK_TIMEOUT_SECONDS:-240}"
report_dir="${COORDINATOR_REPORT_DIR:-reports/agentic}"
port_forward_pid=""
load_pid=""
original_server_address=""

mkdir -p "${report_dir}"

restore() {
  [[ -z "${load_pid}" ]] || kill "${load_pid}" >/dev/null 2>&1 || true
  [[ -z "${port_forward_pid}" ]] \
    || kill "${port_forward_pid}" >/dev/null 2>&1 || true
  if [[ -n "${original_server_address}" ]]; then
    kubectl -n "${namespace}" patch scaledobject "${scaled_object}" \
      --type json \
      --field-manager=helm \
      -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_server_address}\"}]" \
      >/dev/null || true
  fi
}
trap restore EXIT INT TERM

wait_for_available() {
  local expected="$1" mode="$2" timeout_seconds="$3"
  local deadline=$((SECONDS + timeout_seconds)) available
  while ((SECONDS < deadline)); do
    available="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.status.availableReplicas}')"
    printf '%s %s available=%s expected-%s=%s\n' \
      "$(date +%T)" "${deployment}" "${available:-0}" "${mode}" "${expected}"
    if [[ "${mode}" == at-least && "${available:-0}" -ge "${expected}" ]]; then
      return 0
    fi
    if [[ "${mode}" == exactly && "${available:-0}" == "${expected}" ]]; then
      return 0
    fi
    sleep 5
  done
  return 1
}

kubectl -n "${namespace}" wait --for=condition=Ready \
  sandboxagent/recsys-coordinator-agent-sandbox --timeout=10m
kubectl -n "${namespace}" get scaledobject "${scaled_object}" -o json \
  | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]
assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 3)
assert spec["fallback"]["failureThreshold"] == 3
assert spec["fallback"]["replicas"] == 1
'

wait_for_available 1 exactly "${scale_down_timeout}"

kubectl -n "${namespace}" port-forward service/kagent-controller \
  "${local_port}:8083" >"${report_dir}/coordinator-autoscale-port-forward.log" 2>&1 &
port_forward_pid=$!
card_url="http://127.0.0.1:${local_port}/api/a2a-sandboxes/${namespace}/recsys-coordinator-agent-sandbox/.well-known/agent-card.json"
for _ in $(seq 1 30); do
  if curl -fsS "${card_url}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "${card_url}" >/dev/null

python3 - "${local_port}" "${request_count}" "${concurrency}" \
  "${request_timeout}" <<'PY' >"${report_dir}/coordinator-autoscale-load.json" &
import concurrent.futures
import json
import sys
import urllib.request
import uuid

port, count, concurrency, timeout = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
)
url = (
    f"http://127.0.0.1:{port}/api/a2a-sandboxes/kagent/"
    "recsys-coordinator-agent-sandbox/"
)


def invoke(index):
    request_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {"message": {
            "messageId": request_id,
            "contextId": request_id,
            "role": "user",
            "parts": [{
                "kind": "text",
                "text": f"Reply with exactly COORDINATOR-OK-{index}; do not call tools.",
            }],
        }},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
        return response.status == 200 and not body.get("error")
    except Exception:
        return False


with concurrent.futures.ThreadPoolExecutor(
    max_workers=min(concurrency, count)
) as executor:
    outcomes = list(executor.map(invoke, range(count)))
result = {
    "requests": count,
    "successes": outcomes.count(True),
    "errors": outcomes.count(False),
}
print(json.dumps(result, sort_keys=True))
if not any(outcomes):
    raise SystemExit(1)
PY
load_pid=$!

wait_for_available 3 at-least "${scale_out_timeout}"
wait "${load_pid}"
load_pid=""
kill "${port_forward_pid}" >/dev/null 2>&1 || true
wait "${port_forward_pid}" >/dev/null 2>&1 || true
port_forward_pid=""

wait_for_available 1 exactly "${scale_down_timeout}"

original_server_address="$(kubectl -n "${namespace}" get scaledobject \
  "${scaled_object}" -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
kubectl -n "${namespace}" patch scaledobject "${scaled_object}" --type json \
  --field-manager=coordinator-autoscale-fault \
  -p '[{"op":"replace","path":"/spec/triggers/0/metadata/serverAddress","value":"http://127.0.0.1:1"}]' \
  >/dev/null

deadline=$((SECONDS + fallback_timeout))
fallback=""
desired=""
while ((SECONDS < deadline)); do
  fallback="$(kubectl -n "${namespace}" get scaledobject "${scaled_object}" \
    -o jsonpath='{.status.conditions[?(@.type=="Fallback")].status}')"
  desired="$(kubectl -n "${namespace}" get hpa "${hpa}" \
    -o jsonpath='{.status.desiredReplicas}')"
  printf '%s fallback=%s desired=%s\n' \
    "$(date +%T)" "${fallback:-False}" "${desired:-unknown}"
  [[ "${fallback}" == True && "${desired}" == 1 ]] && break
  sleep 5
done
[[ "${fallback}" == True && "${desired}" == 1 ]]

kubectl -n "${namespace}" patch scaledobject "${scaled_object}" --type json \
  --field-manager=helm \
  -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_server_address}\"}]" \
  >/dev/null
original_server_address=""

kubectl -n "${namespace}" get \
  "workerpool/${worker_pool}" \
  "scaledobject/${scaled_object}" \
  "hpa/${hpa}" -o wide
echo "Coordinator WorkerPool autoscale 1 -> 3 -> 1 and fallback=1 checks passed."
