#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/lib/common.sh
source jenkins/scripts/deploy/agentic.sh

service="${1:?usage: $0 <featureRag|recommendation>}"
timeout="${MCP_AUTH_FRESH_SMOKE_TIMEOUT:-600s}"
[[ -n "${RECSYS_FRESH_SESSION_ID:-}" ]] || {
  printf 'RECSYS_FRESH_SESSION_ID is required; invoke through mcp_auth_rotation_gate.sh\n' >&2
  exit 2
}

case "${service}" in
  featureRag)
    agentic_a2a_smoke recsys-context-agent-sandbox
    ;;
  recommendation)
    recommendation_a2a_smoke
    ;;
  *)
    printf 'unsupported MCP auth service: %s\n' "${service}" >&2
    exit 2
    ;;
esac
