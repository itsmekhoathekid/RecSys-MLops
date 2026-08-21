#!/usr/bin/env bash
set -Eeuo pipefail

namespace="${KAGENT_NAMESPACE:-kagent}"
duration="${AGENTIC_LOAD_DURATION_SECONDS:-300}"
rps="${AGENTIC_LOAD_RPS:-20}"
chunk_id="${AGENTIC_SMOKE_CHUNK_ID:-800080:review:rev_800080_02:0}"
poll_seconds="${AGENTIC_KEDA_POLL_SECONDS:-15}"
decision_grace_seconds="${AGENTIC_SCALE_DECISION_GRACE_SECONDS:-15}"
load_log="${AGENTIC_LOAD_LOG:-reports/agentic/autoscale-load.json}"
load_ready_log="${load_log%.json}.ready"
load_pid=""
a2a_pf_pid=""

mkdir -p "$(dirname "${load_log}")"
cleanup() {
  [[ -n "${load_pid}" ]] && kill "${load_pid}" >/dev/null 2>&1 || true
  [[ -n "${a2a_pf_pid}" ]] && kill "${a2a_pf_pid}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

wait_for_available_replicas() {
  local deployment="$1"
  local minimum="$2"
  local replicas
  for _ in $(seq 1 150); do
    replicas="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.status.availableReplicas}')"
    if [[ "${replicas:-0}" -ge "${minimum}" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "${deployment} did not reach ${minimum} available replicas." >&2
  return 1
}

kubectl -n "${namespace}" rollout status deployment/recsys-feature-rag-mcp \
  --timeout=10m
kubectl -n "${namespace}" exec deployment/recsys-feature-rag-mcp -c mcp -- \
  python -c '
import asyncio
import json
import os
import statistics
import sys
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

duration, rps, chunk_id = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
latencies = []
errors = 0

worker_count = rps * 3
target_interval = worker_count / rps
start_event = asyncio.Event()
ready_event = asyncio.Event()
ready_count = 0
deadline = 0.0

async def worker(worker_id):
    global errors, ready_count
    headers = {"Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"]}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8080/mcp", http_client=http_client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                ready_count += 1
                if ready_count == worker_count:
                    ready_event.set()
                await start_event.wait()
                while time.monotonic() < deadline:
                    started = time.perf_counter()
                    try:
                        result = await session.call_tool(
                            "get_chunk_by_id",
                            {"chunk_id": chunk_id},
                        )
                        if result.isError:
                            errors += 1
                    except Exception:
                        errors += 1
                    finally:
                        latencies.append(time.perf_counter() - started)
                    await asyncio.sleep(
                        max(0, target_interval - (time.perf_counter() - started))
                    )

async def main():
    global deadline
    tasks = [asyncio.create_task(worker(worker_id)) for worker_id in range(worker_count)]
    await asyncio.wait_for(ready_event.wait(), timeout=90)
    deadline = time.monotonic() + duration
    print("READY", file=sys.stderr, flush=True)
    start_event.set()
    await asyncio.gather(*tasks)
    ordered = sorted(latencies)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    result = {
        "requests": len(latencies),
        "errors": errors,
        "error_rate": errors / max(1, len(latencies)),
        "p95_seconds": p95,
    }
    print(json.dumps(result, sort_keys=True))
    if result["error_rate"] >= 0.01 or p95 >= 3:
        raise SystemExit(1)

asyncio.run(main())
' "${duration}" "${rps}" "${chunk_id}" >"${load_log}" 2>"${load_ready_log}" &
load_pid=$!

load_ready=false
for _ in $(seq 1 90); do
  if grep -qx READY "${load_ready_log}"; then
    load_ready=true
    break
  fi
  if ! kill -0 "${load_pid}" >/dev/null 2>&1; then
    wait "${load_pid}"
  fi
  sleep 1
done
[[ "${load_ready}" == "true" ]] || {
  echo "MCP load workers did not initialize within 90 seconds." >&2
  exit 1
}

scaled=false
for _ in 1 2; do
  sleep "${poll_seconds}"
  replicas="$(kubectl -n "${namespace}" get hpa keda-hpa-recsys-feature-rag-mcp \
    -o jsonpath='{.status.desiredReplicas}')"
  if [[ "${replicas:-0}" -ge 3 ]]; then
    scaled=true
    break
  fi
done
if [[ "${scaled}" != "true" ]]; then
  for _ in $(seq 1 "${decision_grace_seconds}"); do
    sleep 1
    replicas="$(kubectl -n "${namespace}" get hpa keda-hpa-recsys-feature-rag-mcp \
      -o jsonpath='{.status.desiredReplicas}')"
    if [[ "${replicas:-0}" -ge 3 ]]; then
      scaled=true
      break
    fi
  done
fi
[[ "${scaled}" == "true" ]] || {
  echo "MCP did not scale to at least 3 replicas within two KEDA polling cycles." >&2
  exit 1
}
wait_for_available_replicas recsys-feature-rag-mcp 3
wait "${load_pid}"
load_pid=""

a2a_port="${AGENTIC_A2A_LOAD_PORT:-18084}"
a2a_requests="${AGENTIC_AGENT_LOAD_REQUESTS:-20}"
kubectl -n "${namespace}" port-forward service/kagent-controller \
  "${a2a_port}:8083" >reports/agentic/autoscale-a2a-port-forward.log 2>&1 &
a2a_pf_pid=$!
for _ in $(seq 1 30); do
  if curl -fsS \
    "http://127.0.0.1:${a2a_port}/api/a2a-sandboxes/kagent/recsys-context-agent-sandbox/.well-known/agent-card.json" \
    >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
python3 - "${a2a_port}" "${a2a_requests}" <<'PY' \
  >reports/agentic/autoscale-agent-load.json &
import concurrent.futures
import json
import sys
import urllib.request
import uuid

port, count = sys.argv[1], int(sys.argv[2])
url = f"http://127.0.0.1:{port}/api/a2a-sandboxes/kagent/recsys-context-agent-sandbox/"

def invoke(index):
    request_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": request_id,
                "contextId": request_id,
                "role": "user",
                "parts": [{
                    "kind": "text",
                    "text": f"Reply with exactly OK-{index}; do not call tools.",
                }],
            },
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.load(response)
    return not bool(body.get("error"))

with concurrent.futures.ThreadPoolExecutor(max_workers=count) as executor:
    outcomes = list(executor.map(invoke, range(count)))
result = {"requests": count, "successes": sum(outcomes)}
print(json.dumps(result, sort_keys=True))
if not all(outcomes):
    raise SystemExit(1)
PY
load_pid=$!

sandbox_scaled=false
for _ in 1 2; do
  sleep "${poll_seconds}"
  replicas="$(kubectl -n "${namespace}" get hpa keda-hpa-recsys-context-sandbox-pool \
    -o jsonpath='{.status.desiredReplicas}')"
  if [[ "${replicas:-0}" -ge 3 ]]; then
    sandbox_scaled=true
    break
  fi
done
if [[ "${sandbox_scaled}" != "true" ]]; then
  for _ in $(seq 1 "${decision_grace_seconds}"); do
    sleep 1
    replicas="$(kubectl -n "${namespace}" get hpa keda-hpa-recsys-context-sandbox-pool \
      -o jsonpath='{.status.desiredReplicas}')"
    if [[ "${replicas:-0}" -ge 3 ]]; then
      sandbox_scaled=true
      break
    fi
  done
fi
[[ "${sandbox_scaled}" == "true" ]] || {
  echo "Sandbox WorkerPool did not scale to at least 3 replicas within two KEDA polling cycles." >&2
  exit 1
}
wait_for_available_replicas recsys-context-sandbox-pool-deployment 3
wait "${load_pid}"
load_pid=""
kill "${a2a_pf_pid}" >/dev/null 2>&1 || true
wait "${a2a_pf_pid}" >/dev/null 2>&1 || true
a2a_pf_pid=""

original_address="$(kubectl -n "${namespace}" get scaledobject recsys-feature-rag-mcp \
  -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
restore_scaler() {
  kubectl -n "${namespace}" patch scaledobject recsys-feature-rag-mcp --type json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_address}\"}]" \
    >/dev/null
}
trap 'restore_scaler; cleanup' EXIT
kubectl -n "${namespace}" patch scaledobject recsys-feature-rag-mcp --type json \
  -p '[{"op":"replace","path":"/spec/triggers/0/metadata/serverAddress","value":"http://unreachable.invalid:9090"}]' \
  >/dev/null

fallback=false
for _ in $(seq 1 6); do
  sleep "${poll_seconds}"
  replicas="$(kubectl -n "${namespace}" get deployment recsys-feature-rag-mcp \
    -o jsonpath='{.status.availableReplicas}')"
  if [[ "${replicas:-0}" == 2 ]]; then
    fallback=true
    break
  fi
done
[[ "${fallback}" == "true" ]] || {
  echo "KEDA fallback did not converge to 2 replicas." >&2
  exit 1
}
restore_scaler
trap cleanup EXIT

original_sandbox_address="$(kubectl -n "${namespace}" get scaledobject recsys-context-sandbox-pool \
  -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
restore_sandbox_scaler() {
  kubectl -n "${namespace}" patch scaledobject recsys-context-sandbox-pool --type json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_sandbox_address}\"}]" \
    >/dev/null
}
trap 'restore_sandbox_scaler; cleanup' EXIT
kubectl -n "${namespace}" patch scaledobject recsys-context-sandbox-pool --type json \
  -p '[{"op":"replace","path":"/spec/triggers/0/metadata/serverAddress","value":"http://unreachable.invalid:9090"}]' \
  >/dev/null
sandbox_fallback=false
for _ in $(seq 1 6); do
  sleep "${poll_seconds}"
  replicas="$(kubectl -n "${namespace}" get deployment recsys-context-sandbox-pool-deployment \
    -o jsonpath='{.status.availableReplicas}')"
  if [[ "${replicas:-0}" == 2 ]]; then
    sandbox_fallback=true
    break
  fi
done
[[ "${sandbox_fallback}" == "true" ]] || {
  echo "Sandbox WorkerPool KEDA fallback did not converge to 2 replicas." >&2
  exit 1
}
restore_sandbox_scaler
trap cleanup EXIT
echo "Agentic MCP and Sandbox WorkerPool autoscale/fallback checks passed."
