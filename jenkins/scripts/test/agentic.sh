#!/usr/bin/env bash

test_feature_rag_mcp() {
  local expected_image
  expected_image="$(resolve_release_image recsys-feature-rag-mcp)"
  component_test_wait_deployment kagent recsys-feature-rag-mcp
  kubectl -n kagent exec deployment/recsys-feature-rag-mcp -c mcp -- \
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
  kubectl -n kagent get scaledobject recsys-feature-rag-mcp \
    -o jsonpath='{.spec.minReplicaCount}{" "}{.spec.maxReplicaCount}{" "}{.spec.fallback.replicas}{"\n"}' \
    | grep -Fx '2 6 2'
}

test_context_agent() {
  kubectl -n kagent wait --for=condition=Ready agent/recsys-context-agent \
    --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-context-agent-sandbox \
    --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
  component_test_wait_deployment kagent recsys-context-agent
  kubectl -n kagent get deployment recsys-context-agent \
    -o jsonpath='{.status.availableReplicas}{"\n"}' | awk '$1 >= 2'
  kubectl -n kagent get scaledobject recsys-context-agent \
    -o jsonpath='{.spec.minReplicaCount}{" "}{.spec.maxReplicaCount}{" "}{.spec.fallback.replicas}{"\n"}' \
    | grep -Fx '2 6 2'
  kubectl -n kagent get workerpool recsys-context-sandbox-pool -o json \
    | python3 -c '
import json, sys
payload = json.load(sys.stdin)
spec = payload["spec"]
assert spec["replicas"] == 2
assert spec.get("sandboxClass", "gvisor") == "gvisor"
assert "ateom-gvisor:v0.0.6" in spec["ateomImage"]
'
  agentic_a2a_smoke recsys-context-agent
  agentic_a2a_smoke recsys-context-agent-sandbox
}
