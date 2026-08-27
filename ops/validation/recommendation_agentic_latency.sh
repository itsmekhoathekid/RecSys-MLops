#!/usr/bin/env bash
set -euo pipefail

namespace="${RECOMMENDATION_NAMESPACE:-kagent}"
direct_port="${RECOMMENDATION_DIRECT_LOCAL_PORT:-18086}"
mcp_port="${RECOMMENDATION_MCP_LOCAL_PORT:-18087}"
a2a_port="${RECOMMENDATION_A2A_LOCAL_PORT:-18085}"
pids=()

cleanup() {
  local pid
  for pid in "${pids[@]}"; do
    kill "${pid}" >/dev/null 2>&1 || true
    wait "${pid}" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT INT TERM

mkdir -p reports/agentic
kubectl -n api-serving port-forward service/recsys-inference-api \
  "${direct_port}:80" >reports/agentic/recommendation-direct-port-forward.log 2>&1 &
pids+=("$!")
kubectl -n "${namespace}" port-forward service/recsys-recommendation-mcp \
  "${mcp_port}:8080" >reports/agentic/recommendation-mcp-port-forward.log 2>&1 &
pids+=("$!")
kubectl -n "${namespace}" port-forward service/kagent-controller \
  "${a2a_port}:8083" >reports/agentic/recommendation-a2a-port-forward.log 2>&1 &
pids+=("$!")
sleep 3

token="$(kubectl -n "${namespace}" get secret recsys-recommendation-mcp-auth \
  -o jsonpath='{.data.MCP_AUTH_TOKEN}' | base64 --decode)"

MCP_AUTH_TOKEN="${token}" python3 - \
  "${direct_port}" "${mcp_port}" "${a2a_port}" \
  "${RECOMMENDATION_SMOKE_USER_ID:-1001}" \
  "${RECOMMENDATION_LATENCY_REQUESTS:-20}" \
  "${RECOMMENDATION_AGENT_LATENCY_REQUESTS:-3}" <<'PY'
import json
import math
import os
import statistics
import sys
import time
import urllib.request
import uuid

direct_port, mcp_port, a2a_port, user_id, request_count, agent_count = sys.argv[1:]
payload = {"user_id": int(user_id), "candidate_item_ids": [800080, 800081], "top_k": 2}


def post(url, body, headers=None, timeout=180):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    return (time.perf_counter() - started) * 1000, result


direct_samples, direct_results = [], []
for _ in range(int(request_count)):
    duration, result = post(f"http://127.0.0.1:{direct_port}/recommendations", payload)
    direct_samples.append(duration)
    direct_results.append(result)

mcp_headers = {
    "Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"],
    "Accept": "application/json, text/event-stream",
}
_, initialized = post(
    f"http://127.0.0.1:{mcp_port}/mcp",
    {
        "jsonrpc": "2.0", "id": "init", "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "latency-proof", "version": "1"}},
    },
    mcp_headers,
)
assert initialized["result"]["protocolVersion"] == "2025-06-18"
mcp_headers["MCP-Protocol-Version"] = "2025-06-18"
mcp_samples, mcp_results = [], []
for index in range(int(request_count)):
    duration, body = post(
        f"http://127.0.0.1:{mcp_port}/mcp",
        {"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {
            "name": "get_personalized_recommendations", "arguments": payload,
        }},
        mcp_headers,
    )
    assert body["result"]["isError"] is False
    content = json.loads(body["result"]["content"][0]["text"])
    mcp_samples.append(duration)
    mcp_results.append(content)

assert all(result["items"] == direct_results[0]["items"] for result in direct_results)
assert all(result["items"] == direct_results[0]["items"] for result in mcp_results)

a2a_samples = []
for _ in range(int(agent_count)):
    request_id = str(uuid.uuid4())
    duration, body = post(
        f"http://127.0.0.1:{a2a_port}/api/a2a-sandboxes/kagent/recsys-recommendation-agent-sandbox/",
        {"jsonrpc": "2.0", "id": request_id, "method": "message/send", "params": {
            "message": {"messageId": request_id, "contextId": request_id,
                        "role": "ROLE_USER", "parts": [{"kind": "text", "text":
                        f"Recommend 2 items for user_id={user_id} from candidates 800080 and 800081."}]},
        }},
    )
    calls = []
    for message in body.get("result", {}).get("history", []):
        for part in message.get("parts", []):
            if part.get("metadata", {}).get("adk_type") == "function_call":
                calls.append(part.get("data", {}).get("name"))
    assert calls == ["get_personalized_recommendations"], calls
    serialized = json.dumps(body)
    assert "recsys-context-agent" not in serialized and "recsys-feature-rag-mcp" not in serialized
    a2a_samples.append(duration)


def p95(samples):
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


direct_p95, mcp_p95, agent_p95 = p95(direct_samples), p95(mcp_samples), p95(a2a_samples)
overhead = mcp_p95 - direct_p95
budget = max(100.0, direct_p95 * 0.10)
evidence = {
    "payload": payload,
    "direct_p95_ms": direct_p95,
    "mcp_p95_ms": mcp_p95,
    "mcp_overhead_ms": overhead,
    "mcp_overhead_budget_ms": budget,
    "agent_p95_ms": agent_p95,
    "direct_requests": len(direct_samples),
    "mcp_requests": len(mcp_samples),
    "agent_requests": len(a2a_samples),
    "no_context_agent_call": True,
}
with open("reports/agentic/recommendation-latency.json", "w", encoding="utf-8") as stream:
    json.dump(evidence, stream, indent=2, sort_keys=True)
print(json.dumps(evidence, indent=2, sort_keys=True))
if overhead > budget:
    raise SystemExit("MCP facade p95 overhead exceeded acceptance budget")
PY
