#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/lib/common.sh
source jenkins/scripts/deploy/agentic.sh

timeout="${COORDINATOR_SMOKE_TIMEOUT:-10m}"
coordinator_agentic_preflight true
agentic_mcp_protocol_smoke
recommendation_mcp_protocol_smoke
coordinator_a2a_smoke

echo "Coordinator regular Agent A2A and direct MCP smoke checks passed."
