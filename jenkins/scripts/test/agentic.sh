#!/usr/bin/env bash

test_feature_rag_mcp() {
  local expected_image workload
  expected_image="$(resolve_release_image recsys-feature-rag-mcp)"
  workload="$(mcp_auth_active_workload featureRag)"
  component_test_wait_deployment kagent "${workload}"
  kubectl -n kagent exec "deployment/${workload}" -c mcp -- \
    python -c '
import json
import sys
import urllib.request

for path in ("/healthz", "/ready", "/version", "/metrics"):
    with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=15) as response:
        assert response.status == 200
        if path == "/version":
            assert json.load(response)["image_reference"] == sys.argv[1]
' "${expected_image}"
  agentic_mcp_protocol_smoke
  kubectl -n kagent get scaledobject "${workload}" \
    -o jsonpath='{.spec.minReplicaCount}{" "}{.spec.maxReplicaCount}{" "}{.spec.fallback.replicas}{"\n"}' \
    | grep -Fx '1 3 1'
}

test_context_agent() {
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-context-agent-sandbox \
    --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
  component_test_wait_deployment kagent recsys-context-sandbox-pool-deployment
  kubectl -n kagent get deployment recsys-context-sandbox-pool-deployment \
    -o jsonpath='{.status.availableReplicas}{"\n"}' | awk '$1 >= 1'
  kubectl -n kagent get scaledobject recsys-context-sandbox-pool -o json \
    | python3 -c '
import json, sys
payload = json.load(sys.stdin)["spec"]
assert payload["scaleTargetRef"] == {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "name": "recsys-context-sandbox-pool-deployment",
}
assert (payload["minReplicaCount"], payload["maxReplicaCount"]) == (2, 3)
fallback = payload["fallback"]
assert (fallback["failureThreshold"], fallback["replicas"]) == (3, 1)
assert fallback.get("behavior", "static") == "static"
'
  kubectl -n kagent get workerpool recsys-context-sandbox-pool -o json \
    | python3 -c '
import json, sys
payload = json.load(sys.stdin)
spec = payload["spec"]
status = payload["status"]
assert spec["replicas"] >= 1
assert status["replicas"] >= 1
assert "ateom-gvisor:v0.0.9" in spec["ateomImage"]
'
  agentic_wait_for_regular_agent_removal
  MCP_AUTH_GATE_EVIDENCE="reports/agentic/mcp-auth-context-fresh-smoke.json" \
    mcp_auth_rollout_gate featureRag smoke \
      bash ops/validation/mcp_auth_fresh_a2a_smoke.sh featureRag
}

test_recommendation_mcp() {
  local expected_image workload
  expected_image="$(resolve_release_image recsys-recommendation-mcp)"
  workload="$(mcp_auth_active_workload recommendation)"
  component_test_wait_deployment kagent "${workload}"
  kubectl -n kagent exec "deployment/${workload}" -c mcp -- \
    python -c '
import json
import sys
import urllib.request

for path in ("/healthz", "/ready", "/version", "/metrics"):
    with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=15) as response:
        assert response.status == 200
        if path == "/version":
            payload = json.load(response)
            assert payload["image_reference"] == sys.argv[1]
            assert payload["downstream"] == "recsys-inference-api"
' "${expected_image}"
  recommendation_mcp_protocol_smoke
  kubectl -n kagent get scaledobject "${workload}" -o json \
    | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]
assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 3)
assert spec["fallback"]["replicas"] == 1
assert spec["scaleTargetRef"] == {
    "apiVersion": "apps/v1", "kind": "Deployment",
    "name": sys.argv[1],
}
' "${workload}"
}

test_recommendation_agent() {
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-recommendation-agent-sandbox \
    --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
  component_test_wait_deployment \
    kagent recsys-recommendation-sandbox-pool-deployment
  kubectl -n kagent get scaledobject recsys-recommendation-sandbox-pool -o json \
    | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]
assert spec["scaleTargetRef"] == {
    "apiVersion": "apps/v1", "kind": "Deployment",
    "name": "recsys-recommendation-sandbox-pool-deployment",
}
assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (2, 2)
assert spec["fallback"]["replicas"] == 1
'
  kubectl -n kagent get sandboxagent recsys-recommendation-agent-sandbox -o yaml \
    | grep -Fq recsys-recommendation-mcp
  if kubectl -n kagent get sandboxagent recsys-recommendation-agent-sandbox -o yaml \
    | grep -Eq 'recsys-context-agent|recsys-feature-rag-mcp'; then
    recsys_error "recommendation agent contains a forbidden context/RAG dependency"
    return 1
  fi
  MCP_AUTH_GATE_EVIDENCE="reports/agentic/mcp-auth-recommendation-fresh-smoke.json" \
    mcp_auth_rollout_gate recommendation smoke \
      bash ops/validation/mcp_auth_fresh_a2a_smoke.sh recommendation
}

test_coordinator_agent() {
  coordinator_agentic_preflight true
  kubectl -n kagent get sandboxagent recsys-coordinator-agent-sandbox -o json \
    | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]
assert "platform" not in spec
assert spec["substrate"]["workerPoolRef"]["name"] == "recsys-coordinator-sandbox-pool"
tools = spec["declarative"]["tools"]
agents = [item["agent"]["name"] for item in tools if item["type"] == "Agent"]
mcps = [item["mcpServer"]["name"] for item in tools if item["type"] == "McpServer"]
assert agents == [
    "recsys-context-agent-sandbox",
    "recsys-recommendation-agent-sandbox",
]
assert mcps == []
'
  kubectl -n kagent get workerpool recsys-coordinator-sandbox-pool -o json \
    | python3 -c '
import json, sys
payload = json.load(sys.stdin)
assert payload["spec"]["replicas"] >= 1
'
  kubectl -n kagent get scaledobject recsys-coordinator-sandbox-pool -o json \
    | python3 -c '
import json, sys
spec = json.load(sys.stdin)["spec"]
assert spec["scaleTargetRef"] == {
    "apiVersion": "apps/v1", "kind": "Deployment",
    "name": "recsys-coordinator-sandbox-pool-deployment",
}
assert (spec["minReplicaCount"], spec["maxReplicaCount"]) == (1, 1)
fallback = spec["fallback"]
assert fallback["failureThreshold"] == 3
assert fallback["replicas"] == 1
assert fallback.get("behavior", "static") == "static"
trigger = spec["triggers"][0]
assert trigger["metricType"] == "AverageValue"
assert trigger["metadata"]["threshold"] == "0.7"
assert "ate_workerpool_workers" in trigger["metadata"]["query"]
assert "ate_worker_state=\"assigned\"" in trigger["metadata"]["query"]
'
  ! kubectl -n kagent get agent recsys-coordinator-agent >/dev/null 2>&1
  coordinator_a2a_smoke
}
