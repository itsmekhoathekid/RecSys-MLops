#!/usr/bin/env bash
set -euo pipefail

namespace="${RECOMMENDATION_NAMESPACE:-kagent}"
timeout="${RECOMMENDATION_TIMEOUT:-600s}"

kubectl -n "${namespace}" rollout status deployment/recsys-recommendation-mcp \
  --timeout="${timeout}"
kubectl -n "${namespace}" wait --for=condition=Ready \
  sandboxagent/recsys-recommendation-agent-sandbox --timeout="${timeout}"
kubectl -n "${namespace}" rollout status \
  deployment/recsys-recommendation-sandbox-pool-deployment --timeout="${timeout}"
kubectl -n "${namespace}" get deployment recsys-recommendation-mcp \
  recsys-recommendation-sandbox-pool-deployment -o wide
kubectl -n "${namespace}" get \
  remotemcpserver/recsys-recommendation-mcp \
  sandboxagent/recsys-recommendation-agent-sandbox \
  workerpool/recsys-recommendation-sandbox-pool
kubectl -n "${namespace}" get scaledobject recsys-recommendation-mcp \
  recsys-recommendation-sandbox-pool

rendered="$(helm template recommendation-proof infra/helm/recsys-recommendation-agent)"
if grep -Eq 'kind: Agent$|recsys-context-agent|recsys-feature-rag-mcp' <<<"${rendered}"; then
  echo "forbidden regular Agent/context/RAG dependency found" >&2
  exit 1
fi
echo "PASS: isolated recommendation MCP + SandboxAgent runtime is Ready"
