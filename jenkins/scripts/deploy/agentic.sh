#!/usr/bin/env bash

agentic_registry_port_forward_pid=""

agentic_registry_open_tunnel() {
  local local_port="${AGENT_REGISTRY_LOCAL_PORT:-12121}"
  local log_file="reports/agentic/agentregistry-port-forward.log"
  mkdir -p reports/agentic
  [[ -z "${agentic_registry_port_forward_pid}" ]] || return 0
  kubectl -n "${AGENT_REGISTRY_NAMESPACE:-agentregistry}" port-forward \
    service/agentregistry "${local_port}:12121" >"${log_file}" 2>&1 &
  agentic_registry_port_forward_pid=$!
  for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${local_port}/openapi.json" \
      >/dev/null 2>&1; then
      return 0
    fi
    if ! kill -0 "${agentic_registry_port_forward_pid}" 2>/dev/null; then
      recsys_error "Agent Registry port-forward terminated early"
      return 1
    fi
    sleep 1
  done
  recsys_error "Agent Registry API did not become ready through port-forward"
  return 1
}

agentic_registry_close_tunnel() {
  if [[ -n "${agentic_registry_port_forward_pid}" ]]; then
    kill "${agentic_registry_port_forward_pid}" >/dev/null 2>&1 || true
    wait "${agentic_registry_port_forward_pid}" >/dev/null 2>&1 || true
    agentic_registry_port_forward_pid=""
  fi
}

agentic_preflight() {
  local include_mcp="${1:-false}"
  local crd
  for crd in sandboxagents.kagent.dev remotemcpservers.kagent.dev \
    workerpools.ate.dev scaledobjects.keda.sh; do
    kubectl get crd "${crd}" >/dev/null
  done
  kubectl get --raw \
    /apis/ate.dev/v1alpha1/namespaces/kagent/workerpools/recsys-context-sandbox-pool/scale \
    >/dev/null
  local scale_selector_path
  scale_selector_path="$(
    kubectl get crd workerpools.ate.dev \
      -o jsonpath='{.spec.versions[?(@.name=="v1alpha1")].subresources.scale.labelSelectorPath}'
  )"
  [[ "${scale_selector_path}" == ".spec.scaleSelector" ]] || {
    recsys_error \
      "WorkerPool /scale is missing .spec.scaleSelector labelSelectorPath"
    return 1
  }
  local worker_pool_selector
  worker_pool_selector="$(
    kubectl -n kagent get workerpool recsys-context-sandbox-pool \
      -o jsonpath='{.spec.scaleSelector}'
  )"
  [[ "${worker_pool_selector}" == \
    "ate.dev/worker-pool=recsys-context-sandbox-pool" ]] || {
    recsys_error "WorkerPool scaleSelector does not match its generated pods"
    return 1
  }
  kubectl get clusterrole keda-operator -o json | python3 -c '
import json, sys
rules = json.load(sys.stdin)["rules"]
assert any(
    "*" in rule.get("apiGroups", [])
    and "*/scale" in rule.get("resources", [])
    and {"patch", "update"}.issubset(rule.get("verbs", []))
    for rule in rules
)
'
  kubectl get clusterrolebinding keda-operator -o json | python3 -c '
import json, sys
binding = json.load(sys.stdin)
assert binding["roleRef"] == {
    "apiGroup": "rbac.authorization.k8s.io",
    "kind": "ClusterRole",
    "name": "keda-operator",
}
assert {
    "kind": "ServiceAccount",
    "name": "keda-operator",
    "namespace": "keda",
} in binding["subjects"]
'
  kubectl -n kagent wait --for=condition=Ready \
    externalsecret/recsys-feature-rag-mcp-auth --timeout="${timeout}"
  kubectl -n kagent get secret recsys-feature-rag-mcp-auth >/dev/null
  kubectl -n api-serving get service recsys-online-feature-api recsys-rag-api >/dev/null
  for service in recsys-online-feature-api recsys-rag-api; do
    local endpoint_ready=false
    for _ in $(seq 1 60); do
      if kubectl -n api-serving get endpointslice \
        -l "kubernetes.io/service-name=${service}" \
        -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[0]}' \
        | grep -Eq '.+'; then
        endpoint_ready=true
        break
      fi
      sleep 2
    done
    [[ "${endpoint_ready}" == "true" ]] || {
      recsys_error "${service} has no Ready EndpointSlice address"
      return 1
    }
  done
  kubectl -n kagent rollout status deployment/kagent-controller \
    --timeout="${timeout}"
  kubectl -n ate-system wait --for=condition=Available deployment --all \
    --timeout="${timeout}"
  kubectl -n agentregistry rollout status deployment/agentregistry \
    --timeout="${timeout}"
  kubectl -n kagent get service kagent-ui >/dev/null
  if [[ "${include_mcp}" == "true" ]]; then
    kubectl -n kagent get service recsys-feature-rag-mcp >/dev/null
  fi
}

recommendation_agentic_preflight() {
  local include_mcp="${1:-false}"
  local crd endpoint_ready=false
  for crd in sandboxagents.kagent.dev remotemcpservers.kagent.dev \
    workerpools.ate.dev scaledobjects.keda.sh; do
    kubectl get crd "${crd}" >/dev/null
  done
  kubectl get --raw \
    /apis/ate.dev/v1alpha1/namespaces/kagent/workerpools/recsys-recommendation-sandbox-pool/scale \
    >/dev/null
  kubectl get clusterrole keda-ate-workerpool-scaler -o json | python3 -c '
import json, sys
rules = json.load(sys.stdin)["rules"]
assert any(
    "ate.dev" in rule.get("apiGroups", [])
    and "workerpools/scale" in rule.get("resources", [])
    and {"get", "patch", "update"}.issubset(rule.get("verbs", []))
    for rule in rules
)
'
  kubectl get clusterrolebinding keda-ate-workerpool-scaler -o json | python3 -c '
import json, sys
binding = json.load(sys.stdin)
assert binding["roleRef"] == {
    "apiGroup": "rbac.authorization.k8s.io",
    "kind": "ClusterRole",
    "name": "keda-ate-workerpool-scaler",
}
assert {
    "kind": "ServiceAccount",
    "name": "keda-operator",
    "namespace": "keda",
} in binding["subjects"]
'
  kubectl -n kagent get workerpool recsys-recommendation-sandbox-pool \
    -o jsonpath='{.spec.scaleSelector}' \
    | grep -Fx 'ate.dev/worker-pool=recsys-recommendation-sandbox-pool'
  kubectl -n kagent wait --for=condition=Ready \
    externalsecret/recsys-recommendation-mcp-auth --timeout="${timeout}"
  kubectl -n kagent get secret recsys-recommendation-mcp-auth >/dev/null
  kubectl -n api-serving get service recsys-inference-api >/dev/null
  for _ in $(seq 1 60); do
    if kubectl -n api-serving get endpointslice \
      -l kubernetes.io/service-name=recsys-inference-api \
      -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[0]}' \
      | grep -Eq '.+'; then
      endpoint_ready=true
      break
    fi
    sleep 2
  done
  [[ "${endpoint_ready}" == "true" ]] || {
    recsys_error "recsys-inference-api has no Ready EndpointSlice address"
    return 1
  }
  if [[ "${include_mcp}" == "true" ]]; then
    kubectl -n kagent get service recsys-recommendation-mcp >/dev/null
  fi
}

coordinator_agentic_preflight() {
  local include_runtime="${1:-false}"
  local endpoint_ready service worker_pool_selector
  agentic_preflight true
  recommendation_agentic_preflight true
  kubectl get --raw \
    /apis/ate.dev/v1alpha1/namespaces/kagent/workerpools/recsys-coordinator-sandbox-pool/scale \
    >/dev/null
  worker_pool_selector="$(
    kubectl -n kagent get workerpool recsys-coordinator-sandbox-pool \
      -o jsonpath='{.spec.scaleSelector}'
  )"
  [[ "${worker_pool_selector}" == \
    "ate.dev/worker-pool=recsys-coordinator-sandbox-pool" ]] || {
    recsys_error "coordinator WorkerPool scaleSelector does not match its pods"
    return 1
  }
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-context-agent-sandbox \
    sandboxagent/recsys-recommendation-agent-sandbox \
    --timeout="${timeout}"
  kubectl -n kagent wait --for=condition=Accepted \
    remotemcpserver/recsys-feature-rag-mcp \
    remotemcpserver/recsys-recommendation-mcp \
    --timeout="${timeout}"
  for service in recsys-feature-rag-mcp recsys-recommendation-mcp; do
    endpoint_ready=false
    for _ in $(seq 1 60); do
      if kubectl -n kagent get endpointslice \
        -l "kubernetes.io/service-name=${service}" \
        -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[0]}' \
        | grep -Eq '.+'; then
        endpoint_ready=true
        break
      fi
      sleep 2
    done
    [[ "${endpoint_ready}" == "true" ]] || {
      recsys_error "${service} has no Ready EndpointSlice address"
      return 1
    }
  done
  if [[ "${include_runtime}" == "true" ]]; then
    kubectl -n kagent wait --for=condition=Ready \
      sandboxagent/recsys-coordinator-agent-sandbox --timeout="${timeout}"
    kubectl -n kagent rollout status \
      deployment/recsys-coordinator-sandbox-pool-deployment \
      --timeout="${timeout}"
  fi
}

recommendation_mcp_protocol_smoke() {
  kubectl -n kagent rollout status deployment/recsys-recommendation-mcp \
    --timeout="${timeout}"
  kubectl -n kagent exec deployment/recsys-recommendation-mcp -c mcp -- python -c '
import asyncio
import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"]}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8080/mcp", http_client=http_client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == [
                    "get_personalized_recommendations"
                ]
                result = await session.call_tool(
                    "get_personalized_recommendations",
                    {"user_id": int(os.getenv("RECOMMENDATION_SMOKE_USER_ID", "1001")), "top_k": 1},
                )
                assert not result.isError

asyncio.run(main())
'
}

recommendation_a2a_smoke() {
  local agent_name="recsys-recommendation-agent-sandbox"
  local user_id="${RECOMMENDATION_SMOKE_USER_ID:-1001}"
  local local_port="${RECOMMENDATION_A2A_LOCAL_PORT:-18085}"
  local base_url="http://127.0.0.1:${local_port}/api/a2a-sandboxes/kagent/${agent_name}"
  local log_file="reports/agentic/${agent_name}-port-forward.log"
  local output_file="reports/agentic/${agent_name}-a2a.json"
  local pid ready=false status=1
  mkdir -p reports/agentic
  kubectl -n kagent port-forward service/kagent-controller \
    "${local_port}:8083" >"${log_file}" 2>&1 &
  pid=$!
  for _ in $(seq 1 30); do
    if curl -fsS "${base_url}/.well-known/agent-card.json" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 1
  done
  if [[ "${ready}" == "true" ]]; then
    for attempt in 1 2 3; do
      if python3 - "${base_url}/" "${user_id}" "${output_file}" <<'PY'
import json
import sys
import urllib.request
import uuid

url, user_id, output_path = sys.argv[1:]
request_id = str(uuid.uuid4())
payload = {
    "jsonrpc": "2.0",
    "id": request_id,
    "method": "message/send",
    "params": {"message": {
        "messageId": request_id,
        "contextId": request_id,
        "role": "user",
        "parts": [{"kind": "text", "text": (
            "Recommend top 3 items for user_id=" + user_id
            + ". Call get_personalized_recommendations exactly once with tool "
            + "arguments {\"user_id\": " + user_id
            + ", \"candidate_item_ids\": null, \"top_k\": 3}. Preserve ranking."
        )}],
    }},
}
request = urllib.request.Request(
    url, data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"}, method="POST",
)
with urllib.request.urlopen(request, timeout=180) as response:
    body = json.load(response)
with open(output_path, "w", encoding="utf-8") as stream:
    json.dump(body, stream, indent=2, sort_keys=True)
if body.get("error"):
    raise SystemExit(body["error"])
result = body.get("result", {})
if result.get("status", {}).get("state") != "completed":
    raise SystemExit("recommendation agent did not complete")
calls = []
responses = []
for message in result.get("history", []):
    for part in message.get("parts", []):
        metadata, data = part.get("metadata", {}), part.get("data", {})
        if metadata.get("adk_type") == "function_call":
            calls.append(data.get("name"))
        elif metadata.get("adk_type") == "function_response":
            responses.append((data.get("name"), data.get("response")))
assert calls == ["get_personalized_recommendations"], calls
assert len(responses) == 1 and responses[0][0] == calls[0], responses
serialized = json.dumps(responses[0][1], sort_keys=True)
assert user_id in serialized and "model_version" in serialized and "items" in serialized
PY
      then
        status=0
        break
      fi
      echo "recommendation A2A smoke attempt ${attempt}/3 failed" >&2
      sleep 2
    done
  fi
  kill "${pid}" >/dev/null 2>&1 || true
  wait "${pid}" >/dev/null 2>&1 || true
  return "${status}"
}

coordinator_a2a_smoke() {
  local agent_name="recsys-coordinator-agent-sandbox"
  local user_id="${COORDINATOR_SMOKE_USER_ID:-1001}"
  local chunk_id="${COORDINATOR_SMOKE_CHUNK_ID:-800080:review:rev_800080_02:0}"
  local local_port="${COORDINATOR_A2A_LOCAL_PORT:-18086}"
  local request_timeout="${COORDINATOR_A2A_REQUEST_TIMEOUT_SECONDS:-420}"
  local selected_cases="${COORDINATOR_SMOKE_CASES:-context_agent,recommendation_agent,composite_agents,direct_mcps,partial_result}"
  local base_url="http://127.0.0.1:${local_port}/api/a2a-sandboxes/kagent/${agent_name}"
  local log_file="reports/agentic/${agent_name}-port-forward.log"
  local output_file="${COORDINATOR_A2A_EVIDENCE_FILE:-reports/agentic/${agent_name}-a2a.json}"
  local pid ready=false status=1
  mkdir -p reports/agentic
  kubectl -n kagent port-forward service/kagent-controller \
    "${local_port}:8083" >"${log_file}" 2>&1 &
  pid=$!
  for _ in $(seq 1 30); do
    if curl -fsS "${base_url}/.well-known/agent-card.json" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 1
  done
  if [[ "${ready}" == "true" ]]; then
    for attempt in 1 2 3; do
      if python3 - "${base_url}/" "${user_id}" "${chunk_id}" \
        "${request_timeout}" "${selected_cases}" \
        "${output_file}" <<'PY'
import json
import sys
import urllib.request
import uuid

url, user_id, chunk_id, request_timeout, selected_cases, output_path = sys.argv[1:]
request_timeout = int(request_timeout)
cases = {
    "context_agent": (
        "Use only the context specialist agent to summarize preferences for "
        f"user {user_id}. Do not call recommendation or direct MCP tools."
    ),
    "recommendation_agent": (
        "Delegate through A2A to SandboxAgent "
        "recsys-recommendation-agent-sandbox to recommend one item "
        f"for user_id={user_id}, with candidate_item_ids=null and top_k=1. "
        "Give the specialist all three values and tell it not to ask for "
        "confirmation. Use no other source."
    ),
    "composite_agents": (
        "Use both specialist agents to recommend three "
        f"items for user {user_id} and explain them with grounded evidence. "
        f"Give the recommendation specialist user_id={user_id}, "
        "candidate_item_ids=null, and top_k=3 without asking for confirmation. "
        "Preserve the recommendation order and cite returned chunk_id values."
    ),
    "direct_mcps": (
        "For independent verification, directly call get_chunk_by_id with "
        f"chunk_id={chunk_id} and get_personalized_recommendations with "
        f"arguments {{\"user_id\":{user_id},\"candidate_item_ids\":null,"
        "\"top_k\":1}}. Do not delegate to an agent."
    ),
    "partial_result": (
        "Directly call get_chunk_by_id with the deliberately nonexistent "
        "chunk_id=coordinator-smoke-missing-chunk and also call "
        "get_personalized_recommendations with arguments "
        f"{{\"user_id\":{user_id},\"candidate_item_ids\":null,\"top_k\":1}}. "
        "Do not delegate or retry. Keep the valid recommendation, repeat its "
        "first item_id exactly, and explicitly say 'context source unavailable'."
    ),
}
requested = [name.strip() for name in selected_cases.split(",") if name.strip()]
unknown = set(requested) - set(cases)
if unknown:
    raise SystemExit(f"unknown coordinator smoke cases: {sorted(unknown)}")
cases = {name: cases[name] for name in requested}

evidence = {}


def invoke(case_name, prompt):
    request_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {"message": {
            "messageId": request_id,
            "contextId": str(uuid.uuid4()),
            "role": "user",
            "parts": [{"kind": "text", "text": prompt}],
        }},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=request_timeout) as response:
        body = json.load(response)
    evidence[case_name] = body
    with open(output_path, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
    if body.get("error"):
        raise SystemExit(f"{case_name}: {body['error']}")
    result = body.get("result", {})
    if result.get("status", {}).get("state") != "completed":
        raise SystemExit(
            f"{case_name} did not complete: "
            + json.dumps(result.get("status", {}), sort_keys=True)
        )
    calls = []
    responses = {}
    for message in result.get("history", []):
        for part in message.get("parts", []):
            metadata, data = part.get("metadata", {}), part.get("data", {})
            if metadata.get("adk_type") == "function_call":
                calls.append(data.get("name", ""))
            elif metadata.get("adk_type") == "function_response":
                responses[data.get("name", "")] = data.get("response")
    if not calls or set(calls) - set(responses):
        raise SystemExit(
            f"{case_name} missing call/response: calls={calls}, responses={responses}"
        )
    if case_name == "context_agent":
        assert any("context_agent_sandbox" in name for name in calls), calls
        assert not any("recommendation_agent_sandbox" in name for name in calls), calls
        assert not any(name.startswith("get_") or name.startswith("retrieve_") or name.startswith("build_") for name in calls), calls
    elif case_name == "recommendation_agent":
        assert any("recommendation_agent_sandbox" in name for name in calls), calls
        assert not any("context_agent_sandbox" in name for name in calls), calls
        assert not any(name.startswith("get_") or name.startswith("retrieve_") or name.startswith("build_") for name in calls), calls
    elif case_name == "composite_agents":
        assert any("context_agent_sandbox" in name for name in calls), calls
        assert any("recommendation_agent_sandbox" in name for name in calls), calls
        assert not any(name.startswith("get_") or name.startswith("retrieve_") or name.startswith("build_") for name in calls), calls
    elif case_name == "direct_mcps":
        assert "get_chunk_by_id" in calls, calls
        assert "get_personalized_recommendations" in calls, calls
        assert not any("agent_sandbox" in name for name in calls), calls
        assert calls.count("get_chunk_by_id") == 1, calls
        assert calls.count("get_personalized_recommendations") == 1, calls
    else:
        assert "get_chunk_by_id" in calls, calls
        assert "get_personalized_recommendations" in calls, calls
        assert not any("agent_sandbox" in name for name in calls), calls
        assert calls.count("get_chunk_by_id") == 1, calls
        assert calls.count("get_personalized_recommendations") == 1, calls
        answer_messages = [
            message
            for message in result.get("history", [])
            if message.get("role") == "agent"
        ]
        if result.get("status", {}).get("message"):
            answer_messages.append(result["status"]["message"])
        final_text = " ".join(
            part.get("text", "")
            for message in answer_messages
            for part in message.get("parts", [])
            if part.get("kind") == "text" and part.get("text")
        ).strip()
        assert "context source unavailable" in final_text.lower(), final_text

        def collect_item_ids(value):
            found = []
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "item_id" and child is not None:
                        found.append(str(child))
                    found.extend(collect_item_ids(child))
            elif isinstance(value, list):
                for child in value:
                    found.extend(collect_item_ids(child))
            return found

        item_ids = collect_item_ids(responses["get_personalized_recommendations"])
        assert item_ids, responses["get_personalized_recommendations"]
        assert item_ids[0] in final_text, final_text
    return body


for name, prompt in cases.items():
    invoke(name, prompt)
PY
      then
        status=0
        break
      fi
      echo "coordinator A2A smoke attempt ${attempt}/3 failed" >&2
      sleep $((attempt * 5))
    done
  fi
  kill "${pid}" >/dev/null 2>&1 || true
  wait "${pid}" >/dev/null 2>&1 || true
  return "${status}"
}

agentic_mcp_protocol_smoke() {
  kubectl -n kagent rollout status deployment/recsys-feature-rag-mcp \
    --timeout="${timeout}"
  kubectl -n kagent exec deployment/recsys-feature-rag-mcp -c mcp -- python -c '
import asyncio
import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"]}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8080/mcp", http_client=http_client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                expected = {
                    "get_user_online_features",
                    "get_chunk_by_id",
                    "retrieve_rag_context",
                    "build_user_rag_context",
                }
                assert {tool.name for tool in tools.tools} == expected

asyncio.run(main())
'
}

agentic_a2a_smoke() {
  local agent_name="$1"
  local chunk_id="${AGENTIC_SMOKE_CHUNK_ID:?AGENTIC_SMOKE_CHUNK_ID is required for grounded A2A smoke}"
  local user_id="${AGENTIC_SMOKE_USER_ID:-1001}"
  local local_port="${AGENTIC_A2A_LOCAL_PORT:-18084}"
  local a2a_path="api/a2a-sandboxes"
  local card_path=".well-known/agent-card.json"
  local base_url="http://127.0.0.1:${local_port}/${a2a_path}/kagent/${agent_name}"
  local log_file response_file pid card_ready=false
  mkdir -p reports/agentic
  log_file="reports/agentic/${agent_name}-port-forward.log"
  response_file="reports/agentic/${agent_name}-a2a.json"
  kubectl -n kagent port-forward service/kagent-controller \
    "${local_port}:8083" >"${log_file}" 2>&1 &
  pid=$!
  for _ in $(seq 1 30); do
    if python3 - "${base_url}/${card_path}" 2>/dev/null <<'PY'
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=2) as response:
    assert response.status == 200
PY
    then
      card_ready=true
      break
    fi
    sleep 1
  done
  if ! kill -0 "${pid}" 2>/dev/null; then
    wait "${pid}" || true
    recsys_error "kagent A2A port-forward failed for ${agent_name}"
    return 1
  fi
  if [[ "${card_ready}" != "true" ]]; then
    kill "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
    recsys_error "kagent A2A agent card did not become ready for ${agent_name}"
    return 1
  fi
  local smoke_status=1
  local attempt
  for attempt in 1 2 3; do
    smoke_status=0
    python3 - "${base_url}/" "${user_id}" "${chunk_id}" "${response_file}" <<'PY' || smoke_status=$?
import json
import sys
import urllib.request
import uuid

url, user_id, chunk_id, output_path = sys.argv[1:]
cases = {
    "get_user_online_features": (
        f"Call get_user_online_features with user_id={user_id}, "
        "candidate_item_ids=[800078,800079], and top_k=2. Then answer "
        f"immediately with one concise final text stating user_id {user_id}."
    ),
    "get_chunk_by_id": (
        f"Call get_chunk_by_id with chunk_id={chunk_id}. Then answer directly "
        f"with one concise final text citing exact chunk_id {chunk_id}."
    ),
    "retrieve_rag_context": (
        "Call retrieve_rag_context for query 'noise-cancelling headphones' "
        "with top_k_items=2. Then answer directly and cite returned chunk_id "
        "values in one concise final text."
    ),
    "build_user_rag_context": (
        f"Call build_user_rag_context with user_id={user_id}, query "
        "'noise-cancelling headphones', candidate_item_ids=[800078,800079], "
        "top_k=2, and top_k_items=2. Then answer directly and cite returned "
        "chunk_id values in one concise final text."
    ),
}
def collect_chunk_ids(value):
    found = set()
    if isinstance(value, dict):
        if isinstance(value.get("chunk_id"), str):
            found.add(value["chunk_id"])
        for child in value.values():
            found.update(collect_chunk_ids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(collect_chunk_ids(child))
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = None
        if decoded is not None and decoded != value:
            found.update(collect_chunk_ids(decoded))
    return found


def invoke(tool_name, prompt):
    request_id = str(uuid.uuid4())
    context_id = str(uuid.uuid4())
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": request_id,
                "contextId": context_id,
                "role": "user",
                "parts": [{"kind": "text", "text": prompt}],
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
    evidence[tool_name] = body
    with open(output_path, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream, indent=2, sort_keys=True)
    if body.get("error"):
        raise SystemExit(f"{tool_name} A2A error: {body['error']}")
    result = body.get("result", {})
    status = result.get("status", {})
    if status.get("state") != "completed":
        raise SystemExit(
            f"{tool_name} A2A task did not complete: "
            + json.dumps(status, sort_keys=True)
        )
    calls = set()
    responses = {}
    for message in result.get("history", []):
        for part in message.get("parts", []):
            metadata = part.get("metadata", {})
            data = part.get("data", {})
            if metadata.get("adk_type") == "function_call":
                calls.add(data.get("name"))
            elif metadata.get("adk_type") == "function_response":
                responses[data.get("name")] = data.get("response")
    if tool_name not in calls or tool_name not in responses:
        raise SystemExit(
            f"{tool_name} call/response missing: calls={sorted(calls)}, "
            f"responses={sorted(responses)}"
        )
    tool_response = responses[tool_name]
    answer_messages = [
        message
        for message in result.get("history", [])
        if message.get("role") == "agent"
    ]
    if status.get("message"):
        answer_messages.append(status["message"])
    final_text = " ".join(
        part.get("text", "")
        for message in answer_messages
        for part in message.get("parts", [])
        if part.get("kind") == "text" and part.get("text")
    ).strip()
    if not final_text:
        raise SystemExit(f"{tool_name} completed without a final text answer")
    if tool_name == "get_user_online_features":
        if user_id not in json.dumps(tool_response, sort_keys=True):
            raise SystemExit("user feature response does not contain user_id")
    else:
        chunk_ids = collect_chunk_ids(tool_response)
        if tool_name == "get_chunk_by_id" and chunk_id not in chunk_ids:
            raise SystemExit("exact chunk response does not contain chunk_id")
        if not chunk_ids:
            raise SystemExit(f"{tool_name} response has no grounded chunk_id")
    return body


evidence = {}
body = {tool_name: invoke(tool_name, prompt) for tool_name, prompt in cases.items()}
with open(output_path, "w", encoding="utf-8") as stream:
    json.dump(body, stream, indent=2, sort_keys=True)
PY
    [[ "${smoke_status}" -ne 0 ]] || break
    sleep $((attempt * 5))
  done
  kill "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
  return "${smoke_status}"
}

agentic_registry_version() {
  local commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  printf '0.1.0+%s\n' "${commit:0:12}"
}

agentic_registry_tag() {
  local version="${1:-$(agentic_registry_version)}"
  # Agent Registry v0.4 tags exclude SemVer's build-metadata '+'. Preserve
  # the requested version verbatim in annotations and use a stable safe tag.
  printf '%s\n' "${version/+/-}"
}

agentic_registry_git_url() {
  local remote
  remote="${AGENT_REGISTRY_GIT_URL:-$(git config --get remote.origin.url)}"
  [[ -n "${remote}" ]] || {
    recsys_error "Agent Registry publish requires AGENT_REGISTRY_GIT_URL or origin"
    return 2
  }
  printf '%s\n' "${remote}"
}

agentic_registry_publish_required() {
  local kind="$1"
  local name="$2"
  local tag="$3"
  local version="$4"
  local commit="$5"
  local output
  output="$(mktemp)"
  arctl get "${kind}" "${name}" --tag "${tag}" -o json >"${output}" 2>/dev/null || {
    rm -f "${output}"
    return 0
  }
  if python3 - "${output}" "${version}" "${commit}" <<'PY'
import json
import sys

path, version, commit = sys.argv[1:]
payload = json.load(open(path, encoding="utf-8"))
serialized = json.dumps(payload, sort_keys=True)
raise SystemExit(0 if version in serialized and commit in serialized else 1)
PY
  then
    rm -f "${output}"
    recsys_log DEPLOY "registry ${kind} ${name}@${version} already matches ${commit}"
    return 1
  fi
  rm -f "${output}"
  recsys_error "registry ${kind} ${name}@${version} exists with different metadata"
  return 2
}

agentic_registry_require_dependency() {
  local kind="$1"
  local name="$2"
  local tag="$3"
  local version="$4"
  local commit="$5"
  local output
  output="$(mktemp)"
  if ! arctl get "${kind}" "${name}" --tag "${tag}" -o json >"${output}"; then
    rm -f "${output}"
    recsys_error "matching registry dependency is required: ${kind} ${name}@${tag}"
    return 1
  fi
  if ! python3 - "${output}" "${version}" "${commit}" <<'PY'
import json
import sys

path, version, commit = sys.argv[1:]
payload = json.load(open(path, encoding="utf-8"))
serialized = json.dumps(payload, sort_keys=True)
raise SystemExit(0 if version in serialized and commit in serialized else 1)
PY
  then
    rm -f "${output}"
    recsys_error "registry dependency metadata does not match ${commit}: ${name}@${tag}"
    return 1
  fi
  rm -f "${output}"
}

agentic_registry_tagged_resource_exists() {
  local kind="$1"
  local name="$2"
  local output="$3"
  arctl get "${kind}" "${name}" --all-tags -o json >"${output}" 2>/dev/null \
    || return 1
  python3 - "${output}" <<'PY' >/dev/null 2>&1
import json
import sys

try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
except (json.JSONDecodeError, OSError):
    raise SystemExit(1)
raise SystemExit(0 if isinstance(payload, list) and payload else 1)
PY
}

agentic_write_registry_manifest() {
  local path="$1"
  local kind="$2"
  local qualified_name="$3"
  local version="$4"
  local tag="$5"
  local commit="$6"
  local git_url="$7"
  python3 - "${path}" "${kind}" "${qualified_name}" "${version}" \
    "${tag}" "${commit}" "${git_url}" <<'PY'
import json
import sys

path, kind, qualified_name, version, tag, commit, git_url = sys.argv[1:]
namespace, name = qualified_name.split("/", 1)
metadata = {
    "namespace": namespace,
    "name": name,
    "tag": tag,
    "labels": {
        "app.kubernetes.io/part-of": "recsys-agentic-context",
        "recsys.dev/git-sha": commit[:12],
    },
    "annotations": {
        "recsys.dev/version": version,
        "recsys.dev/git-commit": commit,
        "recsys.dev/source": git_url,
    },
}
if kind == "mcp":
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "MCPServer",
        "metadata": metadata,
        "spec": {
            "title": "RecSys Feature/RAG MCP",
            "description": "Grounded online-feature, exact-chunk and semantic RAG tools",
            "remote": {
                "type": "streamable-http",
                "url": "http://recsys-feature-rag-mcp.kagent.svc.cluster.local:8080/mcp",
            },
        },
    }
elif kind == "agent":
    variant = "sandbox" if name.endswith("-sandbox") else "regular"
    metadata["labels"]["recsys.dev/variant"] = variant
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Agent",
        "metadata": metadata,
        "spec": {
            "title": f"RecSys Context Agent ({variant})",
            "description": "Grounded personalization, exact chunk and semantic RAG agent",
            "mcpServers": [{
                "kind": "MCPServer",
                "namespace": namespace,
                "name": "recsys-feature-rag-mcp",
                "tag": tag,
            }],
        },
    }
elif kind == "recommendation-mcp":
    metadata["labels"]["app.kubernetes.io/part-of"] = "recsys-recommendation-agentic"
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "MCPServer",
        "metadata": metadata,
        "spec": {
            "title": "RecSys Recommendation MCP",
            "description": "Recommendation-only facade for recsys-inference-api",
            "remote": {
                "type": "streamable-http",
                "url": "http://recsys-recommendation-mcp.kagent.svc.cluster.local:8080/mcp",
            },
        },
    }
elif kind == "recommendation-agent":
    metadata["labels"].update({
        "app.kubernetes.io/part-of": "recsys-recommendation-agentic",
        "recsys.dev/variant": "sandbox",
    })
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Agent",
        "metadata": metadata,
        "spec": {
            "title": "RecSys Recommendation Agent (sandbox)",
            "description": "gVisor agent that presents inference rankings without reranking",
            "mcpServers": [{
                "kind": "MCPServer", "namespace": namespace,
                "name": "recsys-recommendation-mcp", "tag": tag,
            }],
        },
    }
elif kind == "coordinator-agent":
    metadata["labels"].update({
        "app.kubernetes.io/part-of": "recsys-coordinator-agentic",
        "recsys.dev/variant": "sandbox",
    })
    metadata["annotations"]["recsys.dev/a2a-dependencies"] = ",".join([
        f"recsys/recsys-context-agent-sandbox@{tag}",
        f"recsys/recsys-recommendation-agent-sandbox@{tag}",
    ])
    resource = {
        "apiVersion": "ar.dev/v1alpha1",
        "kind": "Agent",
        "metadata": metadata,
        "spec": {
            "title": "RecSys Coordinator Agent (sandbox)",
            "description": (
                "Intent-routing gVisor coordinator for context, RAG, and "
                "recommendation specialists"
            ),
            "mcpServers": [
                {
                    "kind": "MCPServer",
                    "namespace": namespace,
                    "name": "recsys-feature-rag-mcp",
                    "tag": tag,
                },
                {
                    "kind": "MCPServer",
                    "namespace": namespace,
                    "name": "recsys-recommendation-mcp",
                    "tag": tag,
                },
            ],
        },
    }
else:
    raise SystemExit(f"unsupported registry resource kind: {kind}")
with open(path, "w", encoding="utf-8") as stream:
    json.dump(resource, stream, indent=2, sort_keys=True)
PY
}

agentic_assert_registry_publish_branch() {
  local branch="${BRANCH_NAME:-${GIT_BRANCH:-$(git branch --show-current)}}"
  case "${branch}" in
    main|origin/main|refs/heads/main|refs/remotes/origin/main) ;;
    *)
      if ! git rev-parse --verify origin/main^{commit} >/dev/null 2>&1 ||
        [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; then
        recsys_log DEPLOY "skip Agent Registry publish on non-main branch ${branch:-detached}"
        return 1
      fi
      ;;
  esac
  command -v arctl >/dev/null 2>&1 || {
    recsys_error "arctl is required for Agent Registry publish"
    return 2
  }
}

agentic_write_registry_evidence() {
  local path="$1"
  local version="$2"
  local commit="$3"
  shift 3
  mkdir -p .ci-deploy
  python3 - "${path}" "${version}" "${commit}" "$@" <<'PY'
import json
import sys

path, version, commit, *artifacts = sys.argv[1:]
with open(path, "w", encoding="utf-8") as stream:
    json.dump(
        {"version": version, "git_commit": commit, "artifacts": artifacts},
        stream,
        indent=2,
        sort_keys=True,
    )
PY
}

agentic_wait_for_regular_agent_removal() {
  local resource
  for resource in \
    agent/recsys-context-agent \
    deployment/recsys-context-agent \
    scaledobject/recsys-context-agent \
    hpa/keda-hpa-recsys-context-agent; do
    for _ in $(seq 1 120); do
      if ! kubectl -n kagent get "${resource}" >/dev/null 2>&1; then
        break
      fi
      sleep 2
    done
    if kubectl -n kagent get "${resource}" >/dev/null 2>&1; then
      recsys_error "legacy regular Agent resource still exists: ${resource}"
      return 1
    fi
  done
}

agentic_write_context_registry_evidence() {
  local path="$1"
  local version="$2"
  local commit="$3"
  local active_artifact="$4"
  local backup_path="$5"
  local removed="$6"
  python3 - "${path}" "${version}" "${commit}" "${active_artifact}" \
    "${backup_path}" "${removed}" <<'PY'
import json
import sys

path, version, commit, active, backup, removed = sys.argv[1:]
payload = {
    "version": version,
    "git_commit": commit,
    "artifacts": [active],
    "removed_artifacts": (["recsys/recsys-context-agent@all-tags"] if removed == "true" else []),
    "legacy_registry_backup": backup,
}
with open(path, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
PY
}

publish_feature_rag_mcp_registry() {
  local version tag commit git_url registry_name manifest
  agentic_assert_registry_publish_branch || return 0
  agentic_preflight true
  agentic_mcp_protocol_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="${AGENT_REGISTRY_MCP_NAME:-recsys/recsys-feature-rag-mcp}"
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" mcp "${registry_name}" \
    "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required mcp "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get mcp "${registry_name}" --tag "${tag}" -o json >/dev/null
  agentic_write_registry_evidence \
    .ci-deploy/feature-rag-mcp-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}"
}

publish_context_agent_registry() {
  local version tag commit git_url registry_name manifest
  local legacy_name legacy_backup legacy_check legacy_present=false
  agentic_assert_registry_publish_branch || return 0
  agentic_preflight true
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-context-agent-sandbox --timeout="${timeout}"
  kubectl -n kagent rollout status \
    deployment/recsys-context-sandbox-pool-deployment --timeout="${timeout}"
  agentic_wait_for_regular_agent_removal
  agentic_a2a_smoke recsys-context-agent-sandbox
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="recsys/recsys-context-agent-sandbox"
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" agent "${registry_name}" \
    "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required agent "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get agent "${registry_name}" --tag "${tag}" -o json >/dev/null

  mkdir -p .ci-deploy
  legacy_name="recsys/recsys-context-agent"
  legacy_backup=".ci-deploy/recsys-context-agent-registry-backup.json"
  if agentic_registry_tagged_resource_exists agent "${legacy_name}" \
    "${legacy_backup}"; then
    legacy_present=true
    arctl delete agent "${legacy_name}" --all-tags
  else
    python3 - "${legacy_backup}" <<'PY'
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump([], stream, indent=2, sort_keys=True)
PY
  fi
  legacy_check="$(mktemp)"
  if agentic_registry_tagged_resource_exists agent "${legacy_name}" \
    "${legacy_check}"; then
    rm -f "${legacy_check}"
    recsys_error "legacy regular Agent still exists in Agent Registry"
    return 1
  fi
  rm -f "${legacy_check}"
  agentic_write_context_registry_evidence \
    .ci-deploy/context-agent-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}" "${legacy_backup}" "${legacy_present}"
}

publish_recommendation_mcp_registry() {
  local version tag commit git_url registry_name manifest
  agentic_assert_registry_publish_branch || return 0
  recommendation_agentic_preflight true
  recommendation_mcp_protocol_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="recsys/recsys-recommendation-mcp"
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" recommendation-mcp \
    "${registry_name}" "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required mcp "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get mcp "${registry_name}" --tag "${tag}" -o json >/dev/null
  agentic_write_registry_evidence \
    .ci-deploy/recommendation-mcp-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}"
}

publish_recommendation_agent_registry() {
  local version tag commit git_url registry_name manifest
  agentic_assert_registry_publish_branch || return 0
  recommendation_agentic_preflight true
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-recommendation-agent-sandbox --timeout="${timeout}"
  kubectl -n kagent rollout status \
    deployment/recsys-recommendation-sandbox-pool-deployment --timeout="${timeout}"
  recommendation_a2a_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="recsys/recsys-recommendation-agent-sandbox"
  arctl get mcp recsys/recsys-recommendation-mcp \
    --tag "${tag}" -o json >/dev/null || {
    recsys_error "matching recommendation MCP registry version is required"
    return 1
  }
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" recommendation-agent \
    "${registry_name}" "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required agent "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get agent "${registry_name}" --tag "${tag}" -o json >/dev/null
  agentic_write_registry_evidence \
    .ci-deploy/recommendation-agent-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}"
}

publish_coordinator_agent_registry() {
  local version tag commit git_url registry_name manifest
  agentic_assert_registry_publish_branch || return 0
  coordinator_agentic_preflight true
  agentic_mcp_protocol_smoke
  recommendation_mcp_protocol_smoke
  coordinator_a2a_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  tag="$(agentic_registry_tag "${version}")"
  git_url="$(agentic_registry_git_url)"
  registry_name="recsys/recsys-coordinator-agent-sandbox"
  agentic_registry_require_dependency mcp \
    recsys/recsys-feature-rag-mcp "${tag}" "${version}" "${commit}"
  agentic_registry_require_dependency mcp \
    recsys/recsys-recommendation-mcp "${tag}" "${version}" "${commit}"
  agentic_registry_require_dependency agent \
    recsys/recsys-context-agent-sandbox "${tag}" "${version}" "${commit}"
  agentic_registry_require_dependency agent \
    recsys/recsys-recommendation-agent-sandbox "${tag}" "${version}" "${commit}"
  manifest="$(mktemp)"
  agentic_write_registry_manifest "${manifest}" coordinator-agent \
    "${registry_name}" "${version}" "${tag}" "${commit}" "${git_url}"
  if agentic_registry_publish_required agent "${registry_name}" "${tag}" \
    "${version}" "${commit}"; then
    arctl apply -f "${manifest}"
  else
    [[ "$?" == "1" ]]
  fi
  rm -f "${manifest}"
  arctl get agent "${registry_name}" --tag "${tag}" -o json >/dev/null
  agentic_write_registry_evidence \
    .ci-deploy/coordinator-agent-registry.json "${version}" "${commit}" \
    "${registry_name}@${tag}" \
    "recsys/recsys-context-agent-sandbox@${tag}" \
    "recsys/recsys-recommendation-agent-sandbox@${tag}" \
    "recsys/recsys-feature-rag-mcp@${tag}" \
    "recsys/recsys-recommendation-mcp@${tag}"
}
