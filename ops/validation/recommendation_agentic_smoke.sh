#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/deploy/agentic/rotation.sh

namespace="${RECOMMENDATION_NAMESPACE:-kagent}"
timeout="${RECOMMENDATION_TIMEOUT:-600s}"
mcp_workload="$(mcp_auth_active_workload recommendation)"

kubectl -n "${namespace}" rollout status "deployment/${mcp_workload}" \
  --timeout="${timeout}"
kubectl -n "${namespace}" wait --for=condition=Ready \
  sandboxagent/recsys-recommendation-agent-sandbox --timeout="${timeout}"
kubectl -n "${namespace}" rollout status \
  deployment/recsys-recommendation-sandbox-pool-deployment --timeout="${timeout}"
kubectl -n "${namespace}" get deployment "${mcp_workload}" \
  recsys-recommendation-sandbox-pool-deployment -o wide
kubectl -n "${namespace}" get \
  remotemcpserver/recsys-recommendation-mcp \
  sandboxagent/recsys-recommendation-agent-sandbox \
  workerpool/recsys-recommendation-sandbox-pool
kubectl -n "${namespace}" get scaledobject "${mcp_workload}" \
  recsys-recommendation-sandbox-pool

rendered="$(helm template recommendation-proof infra/helm/recsys-recommendation-agent \
  -f "$(mcp_auth_versions_file)")"
if grep -Eq 'kind: Agent$|recsys-context-agent|recsys-feature-rag-mcp' <<<"${rendered}"; then
  echo "forbidden regular Agent/context/RAG dependency found" >&2
  exit 1
fi
echo "PASS: isolated recommendation MCP + SandboxAgent runtime is Ready"
