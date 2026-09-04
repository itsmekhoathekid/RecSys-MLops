#!/usr/bin/env bash

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
  [[ "${scale_selector_path}" == ".status.selector" ]] || {
    recsys_error \
      "WorkerPool /scale is missing native .status.selector labelSelectorPath"
    return 1
  }
  local worker_pool_selector
  worker_pool_selector="$(
    kubectl -n kagent get workerpool recsys-context-sandbox-pool \
      -o jsonpath='{.status.selector}'
  )"
  [[ -n "${worker_pool_selector}" ]] || {
    recsys_error "WorkerPool native status.selector is empty"
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
    -o jsonpath='{.status.selector}' | grep -Eq '.+'
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
    kubectl get --raw \
      /apis/ate.dev/v1alpha1/namespaces/kagent/workerpools/recsys-coordinator-sandbox-pool/scale \
      >/dev/null
    kubectl -n kagent get workerpool recsys-coordinator-sandbox-pool \
      -o jsonpath='{.status.selector}' | grep -Eq '.+'
    kubectl -n kagent rollout status \
      deployment/recsys-coordinator-sandbox-pool \
      --timeout="${timeout}"
    kubectl -n kagent get scaledobject recsys-coordinator-sandbox-pool >/dev/null
    kubectl -n kagent get hpa keda-hpa-recsys-coordinator-sandbox-pool >/dev/null
  fi
}
