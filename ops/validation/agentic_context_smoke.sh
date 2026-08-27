#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/lib/common.sh
source jenkins/scripts/deploy/agentic.sh

timeout="${AGENTIC_SMOKE_TIMEOUT:-10m}"
mkdir -p reports/agentic

agentic_preflight true
agentic_mcp_protocol_smoke
kubectl -n kagent wait --for=condition=Ready \
  sandboxagent/recsys-context-agent-sandbox --timeout="${timeout}"
kubectl -n kagent rollout status \
  deployment/recsys-context-sandbox-pool --timeout="${timeout}"
agentic_wait_for_regular_agent_removal
agentic_a2a_smoke recsys-context-agent-sandbox

echo "Sandbox-only agentic context smoke passed; evidence is in reports/agentic/."
