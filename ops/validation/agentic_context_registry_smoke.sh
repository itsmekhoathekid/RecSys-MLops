#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/lib/common.sh
source jenkins/scripts/deploy/agentic.sh
command -v arctl >/dev/null 2>&1 || {
  echo "arctl is required." >&2
  exit 2
}

version="${AGENTIC_REGISTRY_VERSION:-0.1.0+$(git rev-parse --short=12 HEAD)}"
tag="${AGENTIC_REGISTRY_TAG:-${version/+/-}}"
commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
mkdir -p reports/agentic
trap agentic_registry_close_tunnel EXIT
agentic_registry_open_tunnel

arctl get mcp recsys/recsys-feature-rag-mcp --tag "${tag}" -o json \
  >reports/agentic/registry-mcp.json
arctl get agent recsys/recsys-context-agent-sandbox --tag "${tag}" -o json \
  >reports/agentic/registry-recsys-context-agent-sandbox.json
legacy_check="$(mktemp)"
if agentic_registry_tagged_resource_exists agent recsys/recsys-context-agent \
  "${legacy_check}"; then
  rm -f "${legacy_check}"
  echo "Legacy regular Agent still exists in Agent Registry." >&2
  exit 1
fi
rm -f "${legacy_check}"
python3 - "${version}" "${commit}" \
  reports/agentic/registry-mcp.json \
  reports/agentic/registry-recsys-context-agent-sandbox.json <<'PY'
import json
import sys

version, commit, *paths = sys.argv[1:]
for path in paths:
    payload = json.load(open(path, encoding="utf-8"))
    serialized = json.dumps(payload, sort_keys=True)
    assert version in serialized, (path, version)
    assert commit in serialized, (path, commit)
PY
echo "Agent Registry contains MCP and SandboxAgent only at ${version} (${tag})."
