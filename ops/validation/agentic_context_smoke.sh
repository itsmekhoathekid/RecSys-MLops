#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/lib/common.sh
source jenkins/scripts/deploy/agentic.sh

timeout="${AGENTIC_SMOKE_TIMEOUT:-10m}"
mkdir -p reports/agentic

agentic_preflight true
agentic_mcp_protocol_smoke
kubectl -n kagent wait --for=condition=Ready agent/recsys-context-agent \
  --timeout="${timeout}"
kubectl -n kagent wait --for=condition=Ready \
  sandboxagent/recsys-context-agent-sandbox --timeout="${timeout}"
agentic_a2a_smoke recsys-context-agent
agentic_a2a_smoke recsys-context-agent-sandbox

echo "Agentic context smoke passed; evidence is in reports/agentic/."
