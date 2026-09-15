#!/usr/bin/env bash

recommendation_a2a_smoke() {
  local agent_name="recsys-recommendation-agent-sandbox"
  local user_id="${RECOMMENDATION_SMOKE_USER_ID:-1001}"
  local local_port="${RECOMMENDATION_A2A_LOCAL_PORT:-18085}"
  local request_timeout="${RECOMMENDATION_A2A_REQUEST_TIMEOUT_SECONDS:-600}"
  local max_attempts="${RECOMMENDATION_A2A_MAX_ATTEMPTS:-1}"
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
    for attempt in $(seq 1 "${max_attempts}"); do
      if python3 - "${base_url}/" "${user_id}" "${request_timeout}" "${output_file}" <<'PY'
import json
import os
import sys
import time
import urllib.request
import uuid

url, user_id, request_timeout, output_path = sys.argv[1:]
request_timeout = int(request_timeout)
request_id = str(uuid.uuid4())
context_id = os.getenv("RECSYS_FRESH_SESSION_ID") or request_id
payload = {
    "jsonrpc": "2.0",
    "id": request_id,
    "method": "SendMessage",
    "params": {"message": {
        "messageId": request_id,
        "contextId": context_id,
        "role": "ROLE_USER",
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
    headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=request_timeout) as response:
    body = json.load(response)
with open(output_path, "w", encoding="utf-8") as stream:
    json.dump(body, stream, indent=2, sort_keys=True)
if body.get("error"):
    raise SystemExit(body["error"])
wire_result = body.get("result", {})
result = wire_result.get("task", wire_result)
if result.get("status", {}).get("state") not in {
    "completed", "TASK_STATE_COMPLETED",
}:
    raise SystemExit("recommendation agent did not complete")
calls = []
call_args = []
responses = []
parts = [
    part
    for container in [*result.get("history", []), *result.get("artifacts", [])]
    for part in container.get("parts", [])
]
for part in parts:
    metadata, data = part.get("metadata", {}), part.get("data", {})
    if metadata.get("adk_type") == "function_call":
        calls.append(data.get("name"))
        call_args.append(data.get("args"))
    elif metadata.get("adk_type") == "function_response":
        responses.append((data.get("name"), data.get("response")))
assert calls == ["get_personalized_recommendations"], calls
assert call_args == [{"user_id": int(user_id), "candidate_item_ids": None, "top_k": 3}], call_args
assert len(responses) == 1 and responses[0][0] == calls[0], responses
serialized = json.dumps(responses[0][1], sort_keys=True)
assert user_id in serialized and "model_version" in serialized and "items" in serialized
session_evidence_path = os.getenv("MCP_AUTH_ROTATION_SESSION_EVIDENCE")
if session_evidence_path:
    with open(session_evidence_path, "w", encoding="utf-8") as stream:
        json.dump({"contextIds": [context_id]}, stream, separators=(",", ":"))
PY
      then
        status=0
        break
      fi
      echo "recommendation A2A smoke attempt ${attempt}/${max_attempts} failed" >&2
      sleep 2
    done
  fi
  recsys_cleanup_process "${pid}"
  return "${status}"
}

coordinator_a2a_smoke() {
  local agent_name="recsys-coordinator-agent-sandbox"
  local user_id="${COORDINATOR_SMOKE_USER_ID:-1001}"
  local chunk_id="${COORDINATOR_SMOKE_CHUNK_ID:-800080:review:rev_800080_02:0}"
  local local_port="${COORDINATOR_A2A_LOCAL_PORT:-18086}"
  # A timed-out nested A2A request keeps running server-side. Retrying the
  # entire six-case suite immediately creates orphan work and amplifies load,
  # so the production registry gate defaults to one longer bounded attempt.
  local request_timeout="${COORDINATOR_A2A_REQUEST_TIMEOUT_SECONDS:-1800}"
  local max_attempts="${COORDINATOR_A2A_MAX_ATTEMPTS:-1}"
  local admission_attempts="${COORDINATOR_A2A_ADMISSION_MAX_ATTEMPTS:-6}"
  local selected_cases="${COORDINATOR_SMOKE_CASES:-context_agent,context_chunk_agent,context_user_rag_agent,recommendation_agent,recommendation_candidates_agent,composite_agents}"
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
    for attempt in $(seq 1 "${max_attempts}"); do
      if python3 - "${base_url}/" "${user_id}" "${chunk_id}" \
        "${request_timeout}" "${admission_attempts}" "${selected_cases}" \
        "${output_file}" <<'PY'
import json
import os
import sys
import time
import urllib.request
import uuid

(
    url,
    user_id,
    chunk_id,
    request_timeout,
    admission_attempts,
    selected_cases,
    output_path,
) = sys.argv[1:]
request_timeout = int(request_timeout)
admission_attempts = int(admission_attempts)
if admission_attempts < 1:
    raise SystemExit("coordinator admission attempts must be positive")
cases = {
    "context_agent": (
        "Call exactly one tool: "
        "kagent__NS__recsys_context_agent_sandbox. Pass it this complete "
        f"request: 'Summarize preferences for user_id={user_id}. Call "
        "get_user_online_features exactly once with arguments "
        f"{{\"user_id\":{user_id},\"candidate_item_ids\":null,\"top_k\":2}}. "
        "Do not ask for confirmation and answer concisely.' Do not call the "
        "recommendation Agent or any MCP tool directly. Answer immediately "
        "after the Context Agent returns."
    ),
    "context_chunk_agent": (
        "Call exactly one tool: "
        "kagent__NS__recsys_context_agent_sandbox. Pass it this complete "
        f"request: 'Call get_chunk_by_id exactly once with chunk_id={chunk_id}. "
        f"Answer concisely and cite exact chunk_id {chunk_id}.' Do not call the "
        "Recommendation Agent or any MCP tool directly. Answer immediately "
        "after the Context Agent returns."
    ),
    "context_user_rag_agent": (
        "Call exactly one tool: "
        "kagent__NS__recsys_context_agent_sandbox. Pass it this complete "
        f"request: 'Call build_user_rag_context exactly once with user_id={user_id}, "
        "query=noise-cancelling headphones, candidate_item_ids=[800078,800079], "
        "top_k=2, top_k_items=2, and filters=null. Answer concisely and cite "
        "returned chunk_id values.' Do not call the Recommendation Agent or any "
        "MCP tool directly. Answer immediately after the Context Agent returns."
    ),
    "recommendation_agent": (
        "Call exactly one tool: "
        "kagent__NS__recsys_recommendation_agent_sandbox. Its request field "
        "must be exactly this JSON object with no surrounding prose: "
        f"'{{\"user_id\":{user_id},\"candidate_item_ids\":null,\"top_k\":1}}'. "
        "Do not call ask_user, the Context Agent, or any MCP tool directly. "
        "Answer immediately after the Recommendation Agent returns."
    ),
    "recommendation_candidates_agent": (
        "Call exactly one tool: "
        "kagent__NS__recsys_recommendation_agent_sandbox. Its request field "
        "must be exactly this JSON object with no surrounding prose: "
        f"'{{\"user_id\":{user_id},\"candidate_item_ids\":[800078,800079],"
        "\"top_k\":1}}'. Do not call ask_user, the Context Agent, or any MCP "
        "tool directly. Answer immediately after the Recommendation Agent returns."
    ),
    "composite_agents": (
        "Use exactly these two specialist Agent tools in order: first "
        "kagent__NS__recsys_recommendation_agent_sandbox, then "
        "kagent__NS__recsys_context_agent_sandbox. Recommend one "
        f"item for user {user_id} and explain it with grounded evidence. "
        "Pass the Recommendation Agent exactly this complete JSON request: "
        f"'{{\"user_id\":{user_id},\"candidate_item_ids\":null,\"top_k\":1}}'. "
        "Tell it to call its recommendation tool immediately, never call "
        "ask_user, and never request confirmation. "
        "After it returns, create a new Context Agent request that names "
        "build_user_rag_context exactly once and includes user_id, the returned "
        "item IDs as candidate_item_ids, query='recommended items', top_k=2, "
        "top_k_items=2, and filters=null. Never forward this original prompt "
        "as the Context Agent request and never let it ask for confirmation. "
        "Never call retrieve_rag_context, build_user_rag_context, or any other "
        "MCP tool directly. Call each specialist exactly once, then answer. "
        "Preserve the recommendation order and cite returned chunk_id values."
    ),
}
requested = [name.strip() for name in selected_cases.split(",") if name.strip()]
unknown = set(requested) - set(cases)
if unknown:
    raise SystemExit(f"unknown coordinator smoke cases: {sorted(unknown)}")
cases = {name: cases[name] for name in requested}

evidence = {}


def invoke(case_name, prompt):
    body = {}
    evidence[case_name] = []
    for admission_attempt in range(1, admission_attempts + 1):
        request_id = str(uuid.uuid4())
        fresh_session = os.getenv("RECSYS_FRESH_SESSION_ID")
        context_id = (
            f"{fresh_session}-{case_name}-{admission_attempt}"
            if fresh_session
            else str(uuid.uuid4())
        )
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "SendMessage",
            "params": {"message": {
                "messageId": request_id,
                "contextId": context_id,
                "role": "ROLE_USER",
                "parts": [{"kind": "text", "text": prompt}],
            }},
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            body = json.load(response)
        evidence[case_name].append(body)
        with open(output_path, "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, indent=2, sort_keys=True)
        if not body.get("error"):
            break
        error_text = json.dumps(body["error"], sort_keys=True).lower()
        if (
            "no free workers" not in error_text
            or admission_attempt == admission_attempts
        ):
            raise SystemExit(f"{case_name}: {body['error']}")
        time.sleep(admission_attempt * 5)
    wire_result = body.get("result", {})
    result = wire_result.get("task", wire_result)
    if result.get("status", {}).get("state") not in {
        "completed", "TASK_STATE_COMPLETED",
    }:
        raise SystemExit(
            f"{case_name} did not complete: "
            + json.dumps(result.get("status", {}), sort_keys=True)
        )
    calls = []
    call_args = []
    responses = {}
    parts = [
        part
        for container in [*result.get("history", []), *result.get("artifacts", [])]
        for part in container.get("parts", [])
    ]
    for part in parts:
        metadata, data = part.get("metadata", {}), part.get("data", {})
        if metadata.get("adk_type") == "function_call":
            calls.append(data.get("name", ""))
            call_args.append(data.get("args", {}))
        elif metadata.get("adk_type") == "function_response":
            responses[data.get("name", "")] = data.get("response")

    answer_containers = [
        *result.get("history", []),
        *result.get("artifacts", []),
    ]
    if result.get("status", {}).get("message"):
        answer_containers.append(result["status"]["message"])
    answer_text = " ".join(
        part.get("text", "")
        for container in answer_containers
        for part in container.get("parts", [])
        if part.get("text")
    ).lower()
    if "clarification" in answer_text:
        raise SystemExit(
            f"{case_name} returned an invalid post-tool clarification: {answer_text}"
        )
    if not calls or set(calls) - set(responses):
        raise SystemExit(
            f"{case_name} missing call/response: calls={calls}, responses={responses}"
        )

    def assert_usable_agent_response(tool_name):
        serialized = json.dumps(responses[tool_name], sort_keys=True).lower()
        rejected = (
            "http_422",
            "tool execution failed",
            "source unavailable",
            "source is unavailable",
        )
        assert not any(marker in serialized for marker in rejected), serialized

    def specialist_request(index):
        args = call_args[index]
        assert set(args) == {"request"}, args
        assert isinstance(args["request"], str), args
        return args["request"]

    if case_name == "context_agent":
        assert calls == ["kagent__NS__recsys_context_agent_sandbox"], calls
        context_tool = calls[0]
        assert_usable_agent_response(context_tool)
        request_text = specialist_request(0)
        assert user_id in request_text and "get_user_online_features" in request_text
    elif case_name == "context_chunk_agent":
        assert calls == ["kagent__NS__recsys_context_agent_sandbox"], calls
        assert_usable_agent_response(calls[0])
        request_text = specialist_request(0)
        assert chunk_id in request_text and "get_chunk_by_id" in request_text
    elif case_name == "context_user_rag_agent":
        assert calls == ["kagent__NS__recsys_context_agent_sandbox"], calls
        assert_usable_agent_response(calls[0])
        request_text = specialist_request(0)
        assert user_id in request_text
        assert "build_user_rag_context" in request_text
        assert "[800078,800079]" in request_text.replace(" ", "")
    elif case_name == "recommendation_agent":
        assert calls == ["kagent__NS__recsys_recommendation_agent_sandbox"], calls
        recommendation_tool = calls[0]
        assert_usable_agent_response(recommendation_tool)
        assert json.loads(specialist_request(0)) == {
            "user_id": int(user_id),
            "candidate_item_ids": None,
            "top_k": 1,
        }
    elif case_name == "recommendation_candidates_agent":
        assert calls == ["kagent__NS__recsys_recommendation_agent_sandbox"], calls
        assert_usable_agent_response(calls[0])
        assert json.loads(specialist_request(0)) == {
            "user_id": int(user_id),
            "candidate_item_ids": [800078, 800079],
            "top_k": 1,
        }
    elif case_name == "composite_agents":
        assert calls == [
            "kagent__NS__recsys_recommendation_agent_sandbox",
            "kagent__NS__recsys_context_agent_sandbox",
        ], calls
        for tool_name in calls:
            assert_usable_agent_response(tool_name)
        assert json.loads(specialist_request(0)) == {
            "user_id": int(user_id),
            "candidate_item_ids": None,
            "top_k": 1,
        }
        context_request = specialist_request(1)
        assert user_id in context_request
        assert "build_user_rag_context" in context_request
    return body


for name, prompt in cases.items():
    invoke(name, prompt)
PY
      then
        status=0
        break
      fi
      echo "coordinator A2A smoke attempt ${attempt}/${max_attempts} failed" >&2
      sleep $((attempt * 5))
    done
  fi
  recsys_cleanup_process "${pid}"
  return "${status}"
}

agentic_a2a_smoke() {
  local agent_name="$1"
  local chunk_id="${AGENTIC_SMOKE_CHUNK_ID:?AGENTIC_SMOKE_CHUNK_ID is required for grounded A2A smoke}"
  local user_id="${AGENTIC_SMOKE_USER_ID:-1001}"
  local local_port="${AGENTIC_A2A_LOCAL_PORT:-18084}"
  local request_timeout="${AGENTIC_A2A_REQUEST_TIMEOUT_SECONDS:-600}"
  local max_attempts="${AGENTIC_A2A_MAX_ATTEMPTS:-1}"
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
    recsys_cleanup_process "${pid}"
    recsys_error "kagent A2A port-forward failed for ${agent_name}"
    return 1
  fi
  if [[ "${card_ready}" != "true" ]]; then
    recsys_cleanup_process "${pid}"
    recsys_error "kagent A2A agent card did not become ready for ${agent_name}"
    return 1
  fi
  local smoke_status=1
  local attempt
  for attempt in $(seq 1 "${max_attempts}"); do
    smoke_status=0
    python3 - "${base_url}/" "${user_id}" "${chunk_id}" \
      "${request_timeout}" "${response_file}" <<'PY' || smoke_status=$?
import json
import os
import sys
import time
import urllib.request
import uuid

url, user_id, chunk_id, request_timeout, output_path = sys.argv[1:]
request_timeout = int(request_timeout)
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


successful_context_ids = []


def invoke(tool_name, prompt):
    body = {}
    for attempt in range(1, 7):
        request_id = str(uuid.uuid4())
        fresh_session = os.getenv("RECSYS_FRESH_SESSION_ID")
        context_id = (
            f"{fresh_session}-{tool_name}-{attempt}"
            if fresh_session
            else str(uuid.uuid4())
        )
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": request_id,
                    "contextId": context_id,
                    "role": "ROLE_USER",
                    "parts": [{"kind": "text", "text": prompt}],
                },
            },
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=request_timeout) as response:
            body = json.load(response)
        evidence[tool_name] = body
        with open(output_path, "w", encoding="utf-8") as stream:
            json.dump(evidence, stream, indent=2, sort_keys=True)
        if not body.get("error"):
            break
        error_text = json.dumps(body["error"], sort_keys=True).lower()
        if "no free workers" not in error_text or attempt == 6:
            raise SystemExit(f"{tool_name} A2A error: {body['error']}")
        time.sleep(attempt * 5)
    wire_result = body.get("result", {})
    result = wire_result.get("task", wire_result)
    status = result.get("status", {})
    if status.get("state") not in {"completed", "TASK_STATE_COMPLETED"}:
        raise SystemExit(
            f"{tool_name} A2A task did not complete: "
            + json.dumps(status, sort_keys=True)
        )
    calls = set()
    responses = {}
    parts = [
        part
        for container in [*result.get("history", []), *result.get("artifacts", [])]
        for part in container.get("parts", [])
    ]
    for part in parts:
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
        if message.get("role") in {"agent", "ROLE_AGENT"}
    ]
    answer_messages.extend(result.get("artifacts", []))
    if status.get("message"):
        answer_messages.append(status["message"])
    final_text = " ".join(
        part.get("text", "")
        for message in answer_messages
        for part in message.get("parts", [])
        if part.get("text")
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
    successful_context_ids.append(context_id)
    return body


evidence = {}
body = {tool_name: invoke(tool_name, prompt) for tool_name, prompt in cases.items()}
with open(output_path, "w", encoding="utf-8") as stream:
    json.dump(body, stream, indent=2, sort_keys=True)
session_evidence_path = os.getenv("MCP_AUTH_ROTATION_SESSION_EVIDENCE")
if session_evidence_path:
    with open(session_evidence_path, "w", encoding="utf-8") as stream:
        json.dump(
            {"contextIds": successful_context_ids},
            stream,
            separators=(",", ":"),
        )
PY
    [[ "${smoke_status}" -ne 0 ]] || break
    sleep $((attempt * 5))
  done
  recsys_cleanup_process "${pid}"
  return "${smoke_status}"
}
