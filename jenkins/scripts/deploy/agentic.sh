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
  for crd in agents.kagent.dev sandboxagents.kagent.dev remotemcpservers.kagent.dev workerpools.ate.dev; do
    kubectl get crd "${crd}" >/dev/null
  done
  kubectl -n kagent wait --for=condition=Ready \
    externalsecret/recsys-feature-rag-mcp-auth --timeout="${timeout}"
  kubectl -n kagent get secret recsys-feature-rag-mcp-auth >/dev/null
  kubectl -n api-serving get service recsys-online-feature-api recsys-rag-api >/dev/null
  for service in recsys-online-feature-api recsys-rag-api; do
    kubectl -n api-serving get endpoints "${service}" \
      -o jsonpath='{.subsets[0].addresses[0].ip}' | grep -E '.+'
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

agentic_mcp_protocol_smoke() {
  kubectl -n kagent rollout status deployment/recsys-feature-rag-mcp \
    --timeout="${timeout}"
  kubectl -n kagent exec deployment/recsys-feature-rag-mcp -c mcp -- python -c '
import asyncio
import os
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"]}
    async with streamable_http_client("http://127.0.0.1:8080/mcp", headers=headers) as streams:
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
  local local_port="${AGENTIC_A2A_LOCAL_PORT:-18083}"
  local base_url="http://127.0.0.1:${local_port}/api/a2a/kagent/${agent_name}"
  local log_file response_file pid card_ready=false
  mkdir -p reports/agentic
  log_file="reports/agentic/${agent_name}-port-forward.log"
  response_file="reports/agentic/${agent_name}-a2a.json"
  kubectl -n kagent port-forward service/kagent-controller \
    "${local_port}:8083" >"${log_file}" 2>&1 &
  pid=$!
  for _ in $(seq 1 30); do
    if python3 - "${base_url}/.well-known/agent.json" <<'PY'
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
  local smoke_status=0
  python3 - "${base_url}/" "${user_id}" "${chunk_id}" "${response_file}" <<'PY' || smoke_status=$?
import json
import sys
import urllib.request
import uuid

url, user_id, chunk_id, output_path = sys.argv[1:]
request_id = str(uuid.uuid4())
prompt = (
    f"Use tools to get user {user_id}, get exact chunk {chunk_id}, and build "
    "user RAG context for query noise-cancelling headphones. Cite chunk_id values; "
    "do not invent unavailable data."
)
payload = {
    "jsonrpc": "2.0",
    "id": request_id,
    "method": "message/send",
    "params": {
        "id": request_id,
        "message": {
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
serialized = json.dumps(body, sort_keys=True)
if body.get("error"):
    raise SystemExit(f"A2A returned error: {body['error']}")
if chunk_id not in serialized:
    raise SystemExit("grounded A2A response does not cite the requested chunk_id")
with open(output_path, "w", encoding="utf-8") as stream:
    json.dump(body, stream, indent=2, sort_keys=True)
PY
  kill "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
  return "${smoke_status}"
}

agentic_registry_version() {
  local commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  printf '0.1.0+%s\n' "${commit:0:12}"
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
  local version="$3"
  local commit="$4"
  local output
  output="$(mktemp)"
  if [[ "${kind}" == "mcp" ]]; then
    arctl mcp show "${name}" --version "${version}" --output json >"${output}" 2>/dev/null || {
      rm -f "${output}"
      return 0
    }
  else
    arctl agent show "${name}" --output json >"${output}" 2>/dev/null || {
      rm -f "${output}"
      return 0
    }
  fi
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

publish_feature_rag_mcp_registry() {
  local version commit git_url
  agentic_assert_registry_publish_branch || return 0
  agentic_preflight true
  agentic_mcp_protocol_smoke
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  git_url="$(agentic_registry_git_url)"
  if agentic_registry_publish_required mcp recsys-feature-rag-mcp "${version}" "${commit}"; then
    arctl mcp publish recsys-feature-rag-mcp \
      --remote-url http://recsys-feature-rag-mcp.kagent.svc.cluster.local:8080/mcp \
      --transport streamable-http \
      --git "${git_url}" \
      --version "${version}" \
      --description "RecSys Feature/RAG MCP; git_commit=${commit}"
  else
    [[ "$?" == "1" ]]
  fi
  agentic_write_registry_evidence \
    .ci-deploy/feature-rag-mcp-registry.json "${version}" "${commit}" \
    recsys-feature-rag-mcp
}

publish_context_agent_registry() {
  local version commit git_url name
  agentic_assert_registry_publish_branch || return 0
  agentic_preflight true
  kubectl -n kagent wait --for=condition=Ready agent/recsys-context-agent \
    --timeout="${timeout}"
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-context-agent-sandbox --timeout="${timeout}"
  agentic_a2a_smoke recsys-context-agent
  agentic_a2a_smoke recsys-context-agent-sandbox
  agentic_registry_open_tunnel
  commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  version="$(agentic_registry_version)"
  git_url="$(agentic_registry_git_url)"
  for name in recsys-context-agent recsys-context-agent-sandbox; do
    if agentic_registry_publish_required agent "${name}" "${version}" "${commit}"; then
      arctl agent publish "${name}" \
        --git "${git_url}" \
        --version "${version}" \
        --description "Grounded RecSys context agent; git_commit=${commit}"
    else
      [[ "$?" == "1" ]]
    fi
  done
  agentic_write_registry_evidence \
    .ci-deploy/context-agent-registry.json "${version}" "${commit}" \
    recsys-context-agent recsys-context-agent-sandbox
}
