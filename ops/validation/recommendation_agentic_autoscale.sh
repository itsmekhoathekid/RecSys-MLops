#!/usr/bin/env bash
set -euo pipefail

namespace="${RECOMMENDATION_NAMESPACE:-kagent}"
target="${1:-all}"
deadline_seconds="${RECOMMENDATION_SCALE_TIMEOUT_SECONDS:-180}"
mcp_load_seconds="${RECOMMENDATION_MCP_LOAD_SECONDS:-90}"
mcp_load_concurrency="${RECOMMENDATION_MCP_LOAD_CONCURRENCY:-8}"
mcp_request_timeout_seconds="${RECOMMENDATION_MCP_REQUEST_TIMEOUT_SECONDS:-60}"
agent_load_requests="${RECOMMENDATION_AGENT_LOAD_REQUESTS:-20}"
agent_load_concurrency="${RECOMMENDATION_AGENT_LOAD_CONCURRENCY:-8}"
agent_load_seconds="${RECOMMENDATION_AGENT_LOAD_SECONDS:-180}"
agent_request_timeout_seconds="${RECOMMENDATION_AGENT_REQUEST_TIMEOUT_SECONDS:-240}"
fallback_timeout_seconds="${RECOMMENDATION_FALLBACK_TIMEOUT_SECONDS:-420}"
decision_grace_seconds="${RECOMMENDATION_SCALE_DECISION_GRACE_SECONDS:-60}"
original_mcp_address=""
original_worker_address=""
mcp_load_pid=""
worker_load_pid=""
port_forward_pid=""

restore() {
  [[ -z "${mcp_load_pid}" ]] || kill "${mcp_load_pid}" >/dev/null 2>&1 || true
  [[ -z "${worker_load_pid}" ]] || kill "${worker_load_pid}" >/dev/null 2>&1 || true
  [[ -z "${port_forward_pid}" ]] || kill "${port_forward_pid}" >/dev/null 2>&1 || true
  if [[ -n "${original_mcp_address}" ]]; then
    kubectl -n "${namespace}" patch scaledobject recsys-recommendation-mcp \
      --type json -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_mcp_address}\"}]" >/dev/null || true
  fi
  if [[ -n "${original_worker_address}" ]]; then
    kubectl -n "${namespace}" patch scaledobject recsys-recommendation-sandbox-pool \
      --type json -p "[{\"op\":\"replace\",\"path\":\"/spec/triggers/0/metadata/serverAddress\",\"value\":\"${original_worker_address}\"}]" >/dev/null || true
  fi
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_three() {
  local deployment="$1" load_pid="$2" deadline=$((SECONDS + deadline_seconds))
  local load_finished=false grace_deadline=0
  while ((SECONDS < deadline)); do
    local available
    available="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.status.availableReplicas}')"
    printf '%s %s available=%s\n' "$(date +%T)" "${deployment}" "${available:-0}"
    [[ "${available:-0}" -ge 3 ]] && return 0
    if [[ "${load_finished}" == "false" ]] && ! kill -0 "${load_pid}" >/dev/null 2>&1; then
      local load_rc
      if wait "${load_pid}"; then
        load_rc=0
      else
        load_rc=$?
      fi
      if [[ "${load_rc}" -ne 0 ]]; then
        echo "load generator exited with code ${load_rc} before ${deployment} reached 3 replicas" >&2
        return 1
      fi
      load_finished=true
      grace_deadline=$((SECONDS + decision_grace_seconds))
      echo "load completed; allowing ${decision_grace_seconds}s for the KEDA polling/rate window"
    fi
    if [[ "${load_finished}" == "true" && "${SECONDS}" -ge "${grace_deadline}" ]]; then
      echo "${deployment} did not reach 3 replicas within the post-load grace window" >&2
      return 1
    fi
    sleep 5
  done
  return 1
}

wait_for_one() {
  local deployment="$1" deadline=$((SECONDS + fallback_timeout_seconds))
  local desired available
  while ((SECONDS < deadline)); do
    desired="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.spec.replicas}')"
    available="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.status.availableReplicas}')"
    printf '%s %s desired=%s available=%s\n' "$(date +%T)" "${deployment}" \
      "${desired:-0}" "${available:-0}"
    [[ "${desired:-0}" == 1 && "${available:-0}" == 1 ]] && return 0
    sleep 5
  done
  echo "${deployment} did not scale down to the 1/1 baseline" >&2
  return 1
}

load_mcp() {
  kubectl -n "${namespace}" exec deployment/recsys-recommendation-mcp -c mcp -- \
    python -c '
import asyncio, json, os, sys, httpx
from time import monotonic
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

duration, concurrency, timeout_seconds = map(int, sys.argv[1:])
attempts = successes = errors = 0
last_error = ""

async def one(client):
    async with streamable_http_client("http://127.0.0.1:8080/mcp", http_client=client) as streams:
        async with ClientSession(streams[0],streams[1]) as session:
            await session.initialize()
            await session.call_tool("get_personalized_recommendations", {"user_id":1001,"top_k":3})

async def worker(deadline):
    global attempts, successes, errors, last_error
    headers={"Authorization":"Bearer "+os.environ["MCP_AUTH_TOKEN"]}
    limits=httpx.Limits(
        max_connections=max(16, concurrency * 2),
        max_keepalive_connections=max(8, concurrency),
    )
    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(timeout_seconds),
        limits=limits,
    ) as client:
        while monotonic() < deadline:
            attempts += 1
            try:
                await one(client)
                successes += 1
            except Exception as exc:
                errors += 1
                last_error = type(exc).__name__
                await asyncio.sleep(0.05)

async def main():
    deadline=monotonic()+duration
    await asyncio.gather(*(worker(deadline) for _ in range(concurrency)))
    print(json.dumps({
        "attempts": attempts,
        "successes": successes,
        "errors": errors,
        "last_error": last_error,
    }, sort_keys=True))
    if successes == 0:
        raise SystemExit(1)
asyncio.run(main())
' "${mcp_load_seconds}" "${mcp_load_concurrency}" \
      "${mcp_request_timeout_seconds}" &
  mcp_load_pid=$!
  wait_for_three recsys-recommendation-mcp "${mcp_load_pid}"
  if kill -0 "${mcp_load_pid}" >/dev/null 2>&1; then
    wait "${mcp_load_pid}"
  fi
  mcp_load_pid=""
}

load_worker() {
  local local_port="${RECOMMENDATION_A2A_LOCAL_PORT:-18085}"
  kubectl -n "${namespace}" port-forward service/kagent-controller \
    "${local_port}:8083" >/tmp/recommendation-a2a-port-forward.log 2>&1 &
  port_forward_pid=$!
  sleep 3
  python3 - "${local_port}" "${agent_load_requests}" \
    "${agent_load_concurrency}" "${agent_request_timeout_seconds}" \
    "${agent_load_seconds}" <<'PY' &
import concurrent.futures, json, sys, urllib.request, uuid
import time
port, requests, concurrency, request_timeout = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
)
duration = int(sys.argv[5])
url=f"http://127.0.0.1:{port}/api/a2a-sandboxes/kagent/recsys-recommendation-agent-sandbox/"

def call(worker_id, index):
    request_id=str(uuid.uuid4())
    body={"jsonrpc":"2.0","id":request_id,"method":"SendMessage","params":{"message":{"messageId":request_id,"contextId":request_id,"role":"ROLE_USER","parts":[{"kind":"text","text":f"Recommend 3 items for user_id=1001. Call get_personalized_recommendations once with arguments {{\"user_id\":1001,\"candidate_item_ids\":null,\"top_k\":3}}. Load worker {worker_id}, request {index}."}]}}}
    request=urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json","A2A-Version":"1.0"},
    )
    try:
        with urllib.request.urlopen(request,timeout=request_timeout) as response:
            return response.status == 200
    except Exception:
        return False

def load_worker(worker_id, deadline):
    offered = successes = errors = 0
    while time.monotonic() < deadline or offered < requests:
        offered += 1
        if call(worker_id, offered):
            successes += 1
        else:
            errors += 1
            time.sleep(0.1)
    return offered, successes, errors

deadline = time.monotonic() + duration
with concurrent.futures.ThreadPoolExecutor(
    max_workers=concurrency
) as pool:
    outcomes=list(pool.map(lambda worker_id: load_worker(worker_id, deadline), range(concurrency)))
result={
    "requests": sum(item[0] for item in outcomes),
    "successes": sum(item[1] for item in outcomes),
    "errors": sum(item[2] for item in outcomes),
    "duration_seconds": duration,
}
print(json.dumps(result, sort_keys=True))
if result["successes"] == 0:
    raise SystemExit(1)
PY
  worker_load_pid=$!
  wait_for_three recsys-recommendation-sandbox-pool "${worker_load_pid}"
  if kill -0 "${worker_load_pid}" >/dev/null 2>&1; then
    wait "${worker_load_pid}"
  fi
  worker_load_pid=""
  kill "${port_forward_pid}" >/dev/null 2>&1 || true
  wait "${port_forward_pid}" >/dev/null 2>&1 || true
  port_forward_pid=""
  wait_for_one recsys-recommendation-sandbox-pool
}

[[ "${target}" == all || "${target}" == mcp ]] && load_mcp
[[ "${target}" == all || "${target}" == worker ]] && load_worker

if [[ "${RECOMMENDATION_PROVE_FALLBACK:-false}" == true ]]; then
  fallback_objects=()
  if [[ "${target}" == all || "${target}" == mcp ]]; then
    original_mcp_address="$(kubectl -n "${namespace}" get scaledobject recsys-recommendation-mcp -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
    fallback_objects+=(recsys-recommendation-mcp)
  fi
  if [[ "${target}" == all || "${target}" == worker ]]; then
    original_worker_address="$(kubectl -n "${namespace}" get scaledobject recsys-recommendation-sandbox-pool -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
    fallback_objects+=(recsys-recommendation-sandbox-pool)
  fi
  for object in "${fallback_objects[@]}"; do
    kubectl -n "${namespace}" patch scaledobject "${object}" --type json \
      -p '[{"op":"replace","path":"/spec/triggers/0/metadata/serverAddress","value":"http://127.0.0.1:1"}]' >/dev/null
  done
  for object in "${fallback_objects[@]}"; do
    deadline=$((SECONDS + fallback_timeout_seconds))
    while ((SECONDS < deadline)); do
      fallback="$(kubectl -n "${namespace}" get scaledobject "${object}" \
        -o jsonpath='{.status.conditions[?(@.type=="Fallback")].status}')"
      desired="$(kubectl -n "${namespace}" get hpa "keda-hpa-${object}" \
        -o jsonpath='{.status.desiredReplicas}')"
      printf '%s %s fallback=%s desired=%s\n' "$(date +%T)" "${object}" \
        "${fallback:-False}" "${desired:-unknown}"
      [[ "${fallback}" == True && "${desired}" == 1 ]] && break
      sleep 5
    done
    [[ "${fallback:-}" == True && "${desired:-}" == 1 ]] || {
      echo "fallback proof failed for ${object}" >&2
      exit 1
    }
  done
  kubectl -n "${namespace}" get scaledobject,hpa | grep recsys-recommendation
fi
