#!/usr/bin/env bash
set -euo pipefail

namespace="${RECOMMENDATION_NAMESPACE:-kagent}"
target="${1:-all}"
deadline_seconds="${RECOMMENDATION_SCALE_TIMEOUT_SECONDS:-180}"
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
trap restore EXIT INT TERM

wait_for_three() {
  local deployment="$1" deadline=$((SECONDS + deadline_seconds))
  while ((SECONDS < deadline)); do
    local available
    available="$(kubectl -n "${namespace}" get deployment "${deployment}" \
      -o jsonpath='{.status.availableReplicas}')"
    printf '%s %s available=%s\n' "$(date +%T)" "${deployment}" "${available:-0}"
    [[ "${available:-0}" -ge 3 ]] && return 0
    sleep 5
  done
  return 1
}

load_mcp() {
  kubectl -n "${namespace}" exec deployment/recsys-recommendation-mcp -c mcp -- \
    python -c '
import asyncio, os, httpx
from time import monotonic
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
async def one(client):
    async with streamable_http_client("http://127.0.0.1:8080/mcp", http_client=client) as streams:
        async with ClientSession(streams[0],streams[1]) as session:
            await session.initialize()
            await session.call_tool("get_personalized_recommendations", {"user_id":1001,"top_k":3})
async def worker(deadline):
    headers={"Authorization":"Bearer "+os.environ["MCP_AUTH_TOKEN"]}
    async with httpx.AsyncClient(headers=headers) as client:
        while monotonic() < deadline:
            await one(client)
async def main():
    duration=int(os.getenv("RECOMMENDATION_MCP_LOAD_SECONDS", "90"))
    concurrency=int(os.getenv("RECOMMENDATION_MCP_LOAD_CONCURRENCY", "8"))
    deadline=monotonic()+duration
    await asyncio.gather(*(worker(deadline) for _ in range(concurrency)))
asyncio.run(main())
' &
  mcp_load_pid=$!
  wait_for_three recsys-recommendation-mcp
  wait "${mcp_load_pid}"
  mcp_load_pid=""
}

load_worker() {
  local local_port="${RECOMMENDATION_A2A_LOCAL_PORT:-18085}"
  kubectl -n "${namespace}" port-forward service/kagent-controller \
    "${local_port}:8083" >/tmp/recommendation-a2a-port-forward.log 2>&1 &
  port_forward_pid=$!
  sleep 3
  python3 - "${local_port}" <<'PY' &
import concurrent.futures, json, sys, urllib.request, uuid
url=f"http://127.0.0.1:{sys.argv[1]}/api/a2a-sandboxes/kagent/recsys-recommendation-agent-sandbox/"
def call(_):
    request_id=str(uuid.uuid4())
    body={"jsonrpc":"2.0","id":request_id,"method":"message/send","params":{"message":{"messageId":request_id,"contextId":request_id,"role":"user","parts":[{"kind":"text","text":"Recommend 3 items for user_id=1001. Call get_personalized_recommendations once with arguments {\"user_id\":1001,\"candidate_item_ids\":null,\"top_k\":3}."}]}}}
    request=urllib.request.Request(url,data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(request,timeout=180) as response: return response.status
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(call, range(12)))
PY
  worker_load_pid=$!
  wait_for_three recsys-recommendation-sandbox-pool-deployment
  wait "${worker_load_pid}"
  worker_load_pid=""
  kill "${port_forward_pid}" >/dev/null 2>&1 || true
  wait "${port_forward_pid}" >/dev/null 2>&1 || true
  port_forward_pid=""
}

[[ "${target}" == all || "${target}" == mcp ]] && load_mcp
[[ "${target}" == all || "${target}" == worker ]] && load_worker

if [[ "${RECOMMENDATION_PROVE_FALLBACK:-false}" == true ]]; then
  original_mcp_address="$(kubectl -n "${namespace}" get scaledobject recsys-recommendation-mcp -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
  original_worker_address="$(kubectl -n "${namespace}" get scaledobject recsys-recommendation-sandbox-pool -o jsonpath='{.spec.triggers[0].metadata.serverAddress}')"
  for object in recsys-recommendation-mcp recsys-recommendation-sandbox-pool; do
    kubectl -n "${namespace}" patch scaledobject "${object}" --type json \
      -p '[{"op":"replace","path":"/spec/triggers/0/metadata/serverAddress","value":"http://127.0.0.1:1"}]' >/dev/null
  done
  for object in recsys-recommendation-mcp recsys-recommendation-sandbox-pool; do
    deadline=$((SECONDS + 240))
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
