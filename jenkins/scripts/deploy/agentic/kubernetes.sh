#!/usr/bin/env bash

agentic_wait_mcp_auth_secrets() {
  local service="$1"
  local namespace="${2:-kagent}"
  local revision secret_name workload_name vault_version
  mcp_auth_validate
  while IFS=$'\t' read -r revision secret_name workload_name vault_version; do
    [[ -n "${revision}" ]] || continue
    kubectl -n "${namespace}" wait --for=condition=Ready \
      "externalsecret/${secret_name}" --timeout="${timeout}"
    if [[ "${vault_version}" != "-" ]]; then
      kubectl -n "${namespace}" get "externalsecret/${secret_name}" -o json \
        | python3 -c '
import json
import sys

expected = sys.argv[1]
spec = json.load(sys.stdin)["spec"]
assert spec["refreshPolicy"] == "CreatedOnce"
assert spec["target"]["immutable"] is True
assert spec["target"]["creationPolicy"] == "Owner"
assert spec["dataFrom"] == [{"extract": {
    "key": sys.argv[2], "version": expected,
}}]
' "${vault_version}" "$(mcp_auth_get_service_field "${service}" vaultPath)"
      [[ "$(kubectl -n "${namespace}" get "secret/${secret_name}" \
        -o jsonpath='{.immutable}')" == "true" ]] || {
        recsys_error "${secret_name} is not an immutable Secret"
        return 1
      }
    else
      kubectl -n "${namespace}" get "secret/${secret_name}" >/dev/null
    fi
  done < <(mcp_auth_list_deployed "${service}")
}

agentic_wait_mcp_services() {
  local service="$1"
  local namespace="${2:-kagent}"
  local revision secret_name workload_name vault_version endpoint_ready
  while IFS=$'\t' read -r revision secret_name workload_name vault_version; do
    [[ -n "${revision}" ]] || continue
    kubectl -n "${namespace}" get "service/${workload_name}" >/dev/null
    endpoint_ready=false
    for _ in $(seq 1 60); do
      if kubectl -n "${namespace}" get endpointslice \
        -l "kubernetes.io/service-name=${workload_name}" \
        -o jsonpath='{.items[*].endpoints[?(@.conditions.ready==true)].addresses[0]}' \
        | grep -Eq '.+'; then
        endpoint_ready=true
        break
      fi
      sleep 2
    done
    [[ "${endpoint_ready}" == "true" ]] || {
      recsys_error "${workload_name} has no Ready EndpointSlice address"
      return 1
    }
  done < <(mcp_auth_list_deployed "${service}")
}

agentic_verify_worker_pool_autoscaling() {
  local worker_pool="$1"
  local deployment="${worker_pool}-deployment"
  local hpa="keda-hpa-${worker_pool}"
  kubectl -n kagent get deployment "${deployment}" -o json | python3 -c '
import json
import sys

pool = sys.argv[1]
deployment = json.load(sys.stdin)
assert any(
    owner.get("apiVersion") == "ate.dev/v1alpha1"
    and owner.get("kind") == "WorkerPool"
    and owner.get("name") == pool
    for owner in deployment["metadata"].get("ownerReferences", [])
)
assert deployment["spec"]["selector"]["matchLabels"] == {
    "ate.dev/worker-pool": pool,
}
' "${worker_pool}"
  kubectl -n kagent get scaledobject "${worker_pool}" -o json | python3 -c '
import json
import sys

pool = sys.argv[1]
scaled = json.load(sys.stdin)
assert scaled["spec"]["scaleTargetRef"] == {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "name": f"{pool}-deployment",
}
' "${worker_pool}"

  local hpa_ready=false
  for _ in $(seq 1 60); do
    if kubectl -n kagent get hpa "${hpa}" -o json | python3 -c '
import json
import sys

pool = sys.argv[1]
hpa = json.load(sys.stdin)
assert hpa["spec"]["scaleTargetRef"] == {
    "apiVersion": "apps/v1",
    "kind": "Deployment",
    "name": f"{pool}-deployment",
}
conditions = {item["type"]: item for item in hpa["status"].get("conditions", [])}
active = conditions.get("ScalingActive", {})
assert active.get("status") == "True", active
assert active.get("reason") != "InvalidSelector", active
' "${worker_pool}" 2>/dev/null; then
      hpa_ready=true
      break
    fi
    sleep 2
  done
  [[ "${hpa_ready}" == true ]] || {
    recsys_error "${hpa} did not become active for ${deployment}"
    return 1
  }
}

agentic_preflight() {
  local include_mcp="${1:-false}"
  local crd
  for crd in sandboxagents.kagent.dev remotemcpservers.kagent.dev \
    workerpools.ate.dev scaledobjects.keda.sh; do
    kubectl get crd "${crd}" >/dev/null
  done
  agentic_verify_worker_pool_autoscaling recsys-context-sandbox-pool
  agentic_wait_mcp_auth_secrets featureRag
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
    agentic_wait_mcp_services featureRag
  fi
}
recommendation_agentic_preflight() {
  local include_mcp="${1:-false}"
  local crd endpoint_ready=false
  for crd in sandboxagents.kagent.dev remotemcpservers.kagent.dev \
    workerpools.ate.dev scaledobjects.keda.sh; do
    kubectl get crd "${crd}" >/dev/null
  done
  agentic_verify_worker_pool_autoscaling recsys-recommendation-sandbox-pool
  agentic_wait_mcp_auth_secrets recommendation
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
    agentic_wait_mcp_services recommendation
  fi
}

coordinator_agentic_preflight() {
  local include_runtime="${1:-false}"
  local endpoint_ready service
  agentic_preflight true
  recommendation_agentic_preflight true
  kubectl -n kagent wait --for=condition=Ready \
    sandboxagent/recsys-context-agent-sandbox \
    sandboxagent/recsys-recommendation-agent-sandbox \
    --timeout="${timeout}"
  kubectl -n kagent wait --for=condition=Accepted \
    remotemcpserver/recsys-feature-rag-mcp \
    remotemcpserver/recsys-recommendation-mcp \
    --timeout="${timeout}"
  agentic_wait_mcp_services featureRag
  agentic_wait_mcp_services recommendation
  if [[ "${include_runtime}" == "true" ]]; then
    kubectl -n kagent wait --for=condition=Ready \
      sandboxagent/recsys-coordinator-agent-sandbox --timeout="${timeout}"
    agentic_verify_worker_pool_autoscaling recsys-coordinator-sandbox-pool
    kubectl -n kagent rollout status \
      deployment/recsys-coordinator-sandbox-pool-deployment \
      --timeout="${timeout}"
    kubectl -n kagent get scaledobject recsys-coordinator-sandbox-pool >/dev/null
    kubectl -n kagent get hpa keda-hpa-recsys-coordinator-sandbox-pool >/dev/null
  fi
}
