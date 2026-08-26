#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/lib/common.sh
source jenkins/scripts/deploy/agentic.sh

namespace="${COORDINATOR_NAMESPACE:-kagent}"
agent="recsys-coordinator-agent"
local_port="${COORDINATOR_CONCURRENCY_LOCAL_PORT:-18087}"
request_count="${COORDINATOR_CONCURRENCY_REQUESTS:-20}"
concurrency="${COORDINATOR_CONCURRENCY:-8}"
request_timeout="${COORDINATOR_CONCURRENCY_REQUEST_TIMEOUT_SECONDS:-240}"
report_dir="${COORDINATOR_REPORT_DIR:-reports/agentic}"
port_forward_pid=""

cleanup() {
  [[ -z "${port_forward_pid}" ]] || kill "${port_forward_pid}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

mkdir -p "${report_dir}"
timeout="10m"
kubectl -n "${namespace}" wait --for=condition=Ready \
  "agent/${agent}" --timeout="${timeout}"
kubectl -n "${namespace}" rollout status \
  "deployment/${agent}" --timeout="${timeout}"
[[ "$(kubectl -n "${namespace}" get deployment "${agent}" \
  -o jsonpath='{.spec.replicas}')" == "1" ]]

kubectl -n "${namespace}" port-forward service/kagent-controller \
  "${local_port}:8083" >"${report_dir}/coordinator-concurrency-port-forward.log" 2>&1 &
port_forward_pid=$!
card_url="http://127.0.0.1:${local_port}/api/a2a/${namespace}/${agent}/.well-known/agent-card.json"
for _ in $(seq 1 30); do
  curl -fsS "${card_url}" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "${card_url}" >/dev/null

python3 - "${local_port}" "${request_count}" "${concurrency}" \
  "${request_timeout}" <<'PY' >"${report_dir}/coordinator-concurrency.json"
import concurrent.futures
import json
import sys
import urllib.request
import uuid

port, count, concurrency, timeout = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
)
url = f"http://127.0.0.1:{port}/api/a2a/kagent/recsys-coordinator-agent/"


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
if not all(outcomes):
    raise SystemExit(1)
PY

[[ "$(kubectl -n "${namespace}" get deployment "${agent}" \
  -o jsonpath='{.spec.replicas}')" == "1" ]]
[[ "$(kubectl -n "${namespace}" get deployment "${agent}" \
  -o jsonpath='{.status.availableReplicas}')" == "1" ]]
! kubectl -n "${namespace}" get workerpool recsys-coordinator-sandbox-pool >/dev/null 2>&1
! kubectl -n "${namespace}" get scaledobject recsys-coordinator-sandbox-pool >/dev/null 2>&1

echo "Coordinator handled concurrent A2A traffic and remained fixed at one replica."
