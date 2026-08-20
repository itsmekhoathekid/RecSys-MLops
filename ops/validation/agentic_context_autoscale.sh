#!/usr/bin/env bash
set -Eeuo pipefail

namespace="${KAGENT_NAMESPACE:-kagent}"
duration="${AGENTIC_LOAD_DURATION_SECONDS:-300}"
rps="${AGENTIC_LOAD_RPS:-20}"
chunk_id="${AGENTIC_SMOKE_CHUNK_ID:-800078:review:rev_800078_01:0}"
poll_seconds="${AGENTIC_KEDA_POLL_SECONDS:-15}"
load_log="${AGENTIC_LOAD_LOG:-reports/agentic/autoscale-load.json}"
load_pid=""
a2a_pf_pid=""

mkdir -p "$(dirname "${load_log}")"
cleanup() {
  [[ -n "${load_pid}" ]] && kill "${load_pid}" >/dev/null 2>&1 || true
  [[ -n "${a2a_pf_pid}" ]] && kill "${a2a_pf_pid}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

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

async def worker(worker_id, deadline):
    global errors
    headers = {"Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"]}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8080/mcp", http_client=http_client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
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
    deadline = time.monotonic() + duration
    await asyncio.gather(
        *(worker(worker_id, deadline) for worker_id in range(worker_count))
    )
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
' "${duration}" "${rps}" "${chunk_id}" >"${load_log}" &
load_pid=$!

scaled=false
for _ in 1 2; do
  sleep "${poll_seconds}"
  replicas="$(kubectl -n "${namespace}" get deployment recsys-feature-rag-mcp \
    -o jsonpath='{.spec.replicas}')"
  if [[ "${replicas:-0}" -ge 3 ]]; then
    scaled=true
    break
  fi
done
[[ "${scaled}" == "true" ]] || {
  echo "MCP did not scale to at least 3 replicas within two KEDA polling cycles." >&2
  exit 1
}
kubectl -n "${namespace}" rollout status deployment/recsys-feature-rag-mcp \
  --timeout=5m
wait "${load_pid}"
load_pid=""

a2a_port="${AGENTIC_A2A_LOAD_PORT:-18084}"
a2a_requests="${AGENTIC_AGENT_LOAD_REQUESTS:-20}"
kubectl -n "${namespace}" port-forward service/kagent-controller \
  "${a2a_port}:8083" >reports/agentic/autoscale-a2a-port-forward.log 2>&1 &
a2a_pf_pid=$!
for _ in $(seq 1 30); do
  if curl -fsS \
    "http://127.0.0.1:${a2a_port}/api/a2a/kagent/recsys-context-agent/.well-known/agent.json" \
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
url = f"http://127.0.0.1:{port}/api/a2a/kagent/recsys-context-agent/"

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
                "parts": [{"kind": "text", "text": f"Get online features for user {1001 + index}."}],
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

agent_scaled=false
for _ in 1 2; do
  sleep "${poll_seconds}"
  replicas="$(kubectl -n "${namespace}" get deployment recsys-context-agent \
    -o jsonpath='{.spec.replicas}')"
  if [[ "${replicas:-0}" -ge 3 ]]; then
    agent_scaled=true
    break
  fi
done
[[ "${agent_scaled}" == "true" ]] || {
  echo "Regular Agent did not scale to at least 3 replicas within two KEDA polling cycles." >&2
  exit 1
}
kubectl -n "${namespace}" rollout status deployment/recsys-context-agent \
  --timeout=5m
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

original_agent_address="$(kubectl -n "${namespace}" get scaledobject recsys-context-agent \
  -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
restore_agent_scaler() {
  kubectl -n "${namespace}" patch scaledobject recsys-context-agent --type json \
    -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_agent_address}\"}]" \
    >/dev/null
}
trap 'restore_agent_scaler; cleanup' EXIT
kubectl -n "${namespace}" patch scaledobject recsys-context-agent --type json \
  -p '[{"op":"replace","path":"/spec/triggers/0/metadata/serverAddress","value":"http://unreachable.invalid:9090"}]' \
  >/dev/null
agent_fallback=false
for _ in $(seq 1 6); do
  sleep "${poll_seconds}"
  replicas="$(kubectl -n "${namespace}" get deployment recsys-context-agent \
    -o jsonpath='{.status.availableReplicas}')"
  if [[ "${replicas:-0}" == 2 ]]; then
    agent_fallback=true
    break
  fi
done
[[ "${agent_fallback}" == "true" ]] || {
  echo "Regular Agent KEDA fallback did not converge to 2 replicas." >&2
  exit 1
}
restore_agent_scaler
trap cleanup EXIT
echo "Agentic MCP and regular Agent autoscale/fallback checks passed."
