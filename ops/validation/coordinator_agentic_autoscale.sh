#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."

namespace="${COORDINATOR_NAMESPACE:-kagent}"
agent="recsys-coordinator-agent-sandbox"
worker_pool="recsys-coordinator-sandbox-pool"
scaled_object="${worker_pool}"
hpa="keda-hpa-${worker_pool}"
local_port="${COORDINATOR_AUTOSCALE_LOCAL_PORT:-18087}"
request_count="${COORDINATOR_AUTOSCALE_REQUESTS:-24}"
concurrency="${COORDINATOR_AUTOSCALE_CONCURRENCY:-12}"
load_duration="${COORDINATOR_AUTOSCALE_LOAD_SECONDS:-180}"
load_stagger="${COORDINATOR_AUTOSCALE_STAGGER_SECONDS:-20}"
request_timeout="${COORDINATOR_AUTOSCALE_REQUEST_TIMEOUT_SECONDS:-420}"
scale_out_timeout="${COORDINATOR_SCALE_OUT_TIMEOUT_SECONDS:-300}"
scale_down_timeout="${COORDINATOR_SCALE_DOWN_TIMEOUT_SECONDS:-480}"
fallback_timeout="${COORDINATOR_FALLBACK_TIMEOUT_SECONDS:-240}"
report_dir="${COORDINATOR_REPORT_DIR:-reports/agentic}"
port_forward_pid=""
load_pid=""
original_server_address=""

mkdir -p "${report_dir}"

restore() {
  [[ -z "${load_pid}" ]] || kill "${load_pid}" >/dev/null 2>&1 || true
  [[ -z "${port_forward_pid}" ]] || kill "${port_forward_pid}" >/dev/null 2>&1 || true
  if [[ -n "${original_server_address}" ]]; then
    kubectl -n "${namespace}" patch scaledobject "${scaled_object}" --type json \
      -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_server_address}\"}]" \
      >/dev/null || true
  fi
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_replicas() {
  local expected="$1" mode="$2" timeout_seconds="$3"
  local deadline=$((SECONDS + timeout_seconds)) available desired
  while ((SECONDS < deadline)); do
    available="$(kubectl -n "${namespace}" get deployment "${worker_pool}" \
      -o jsonpath='{.status.availableReplicas}')"
    desired="$(kubectl -n "${namespace}" get workerpool "${worker_pool}" \
      -o jsonpath='{.spec.replicas}')"
    printf '%s desired=%s available=%s expected-%s=%s\n' \
      "$(date +%T)" "${desired:-0}" "${available:-0}" "${mode}" "${expected}"
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
  "sandboxagent/${agent}" --timeout=10m
kubectl -n "${namespace}" get scaledobject "${scaled_object}" -o json | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]
assert spec["scaleTargetRef"] == {
    "apiVersion": "ate.dev/v1alpha1",
    "kind": "WorkerPool",
    "name": "recsys-coordinator-sandbox-pool",
}
assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 3)
assert spec["pollingInterval"] == 15 and spec["cooldownPeriod"] == 300
assert spec["fallback"]["failureThreshold"] == 3
assert spec["fallback"]["replicas"] == 1
assert spec["fallback"].get("behavior", "static") == "static"
trigger = spec["triggers"][0]
assert trigger["metricType"] == "AverageValue"
metadata = trigger["metadata"]
assert metadata["threshold"] == "0.7"
assert metadata["ignoreNullValues"] == "false"
assert metadata["query"] == (
    "max(ate_workerpool_workers{ate_workerpool_namespace=\"kagent\","
    "ate_workerpool_name=\"recsys-coordinator-sandbox-pool\","
    "ate_worker_state=\"assigned\"})"
)
'

wait_for_replicas 1 exactly "${scale_down_timeout}"

kubectl -n "${namespace}" port-forward service/kagent-controller \
  "${local_port}:8083" >"${report_dir}/coordinator-autoscale-port-forward.log" 2>&1 &
port_forward_pid=$!
base_url="http://127.0.0.1:${local_port}/api/a2a-sandboxes/${namespace}/${agent}"
for _ in $(seq 1 30); do
  curl -fsS "${base_url}/.well-known/agent-card.json" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "${base_url}/.well-known/agent-card.json" >/dev/null

python3 - "${base_url}/" "${request_count}" "${concurrency}" \
  "${request_timeout}" "${load_duration}" "${load_stagger}" \
  >"${report_dir}/coordinator-autoscale-load.json" <<'PY' &
import concurrent.futures
import json
import sys
import time
import urllib.request
import uuid

url, count, concurrency, timeout, duration, stagger = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
    int(sys.argv[5]), int(sys.argv[6])
)

def invoke(worker_id, index):
    request_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "SendMessage",
        "params": {"message": {
            "messageId": request_id,
            "contextId": request_id,
            "role": "ROLE_USER",
            "parts": [{"kind": "text", "text": (
                "Call exactly one tool: "
                "kagent__NS__recsys_recommendation_agent_sandbox. Pass it "
                "this request: 'Recommend top 1 item for user_id=1001. Call "
                "get_personalized_recommendations exactly once with arguments "
                "{\"user_id\":1001,\"candidate_item_ids\":null,\"top_k\":1}. "
                "Do not ask for confirmation.' Do not call any MCP tool "
                f"directly. Load worker {worker_id}, request {index}."
            )}],
        }},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.load(response)
        return response.status == 200 and not body.get("error")
    except Exception:
        return False

def load_worker(worker_id, deadline):
    offered = successes = errors = 0
    # Do not queue every long-running coordinator call against the original
    # single worker. Stagger new sessions so the second worker is Ready before
    # the next request arrives, allowing two workers to be assigned at once.
    time.sleep(worker_id * stagger)
    while time.monotonic() < deadline or offered < count:
        offered += 1
        if invoke(worker_id, offered):
            successes += 1
        else:
            errors += 1
            time.sleep(0.1)
    return offered, successes, errors

deadline = time.monotonic() + duration
with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
    outcomes = list(pool.map(lambda worker_id: load_worker(worker_id, deadline), range(concurrency)))
result = {
    "requests": sum(item[0] for item in outcomes),
    "successes": sum(item[1] for item in outcomes),
    "errors": sum(item[2] for item in outcomes),
    "duration_seconds": duration,
}
print(json.dumps(result, sort_keys=True))
if result["successes"] == 0:
    raise SystemExit(1)
PY
load_pid=$!

seen_two=false
deadline=$((SECONDS + scale_out_timeout))
while ((SECONDS < deadline)); do
  desired="$(kubectl -n "${namespace}" get workerpool "${worker_pool}" -o jsonpath='{.spec.replicas}')"
  metric="$(kubectl -n "${namespace}" get hpa "${hpa}" -o jsonpath='{.status.currentMetrics[0].external.current.averageValue}' 2>/dev/null || true)"
  printf '%s desired=%s assigned-average=%s\n' "$(date +%T)" "${desired:-0}" "${metric:-unknown}"
  [[ "${desired:-0}" -ge 2 ]] && seen_two=true
  [[ "${desired:-0}" -ge 3 ]] && break
  sleep 5
done
[[ "${seen_two}" == true && "${desired:-0}" -ge 3 ]] || {
  echo "Coordinator WorkerPool did not traverse 1 -> 2 -> 3 within ${scale_out_timeout}s." >&2
  exit 1
}
wait_for_replicas 3 at-least "${scale_out_timeout}"
wait "${load_pid}"
load_pid=""

wait_for_replicas 1 exactly "${scale_down_timeout}"

original_server_address="$(kubectl -n "${namespace}" get scaledobject "${scaled_object}" \
  -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
kubectl -n "${namespace}" patch scaledobject "${scaled_object}" --type json \
  -p '[{"op":"replace","path":"/spec/triggers/0/metadata/serverAddress","value":"http://127.0.0.1:1"}]' >/dev/null

deadline=$((SECONDS + fallback_timeout))
fallback=""
desired=""
while ((SECONDS < deadline)); do
  fallback="$(kubectl -n "${namespace}" get scaledobject "${scaled_object}" \
    -o jsonpath='{.status.conditions[?(@.type=="Fallback")].status}')"
  desired="$(kubectl -n "${namespace}" get hpa "${hpa}" -o jsonpath='{.status.desiredReplicas}')"
  printf '%s fallback=%s desired=%s\n' "$(date +%T)" "${fallback:-False}" "${desired:-unknown}"
  [[ "${fallback}" == True && "${desired}" == 1 ]] && break
  sleep 5
done
[[ "${fallback}" == True && "${desired}" == 1 ]]

kubectl -n "${namespace}" patch scaledobject "${scaled_object}" --type json \
  -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_server_address}\"}]" >/dev/null
original_server_address=""

kubectl -n "${namespace}" get "workerpool/${worker_pool}" "scaledobject/${scaled_object}" "hpa/${hpa}" -o wide
echo "Coordinator assigned-worker autoscale 1 -> 2 -> 3 -> 1 and fallback=1 checks passed."
