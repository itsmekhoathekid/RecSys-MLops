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
commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
mkdir -p reports/agentic
trap agentic_registry_close_tunnel EXIT
agentic_registry_open_tunnel

arctl mcp show recsys-feature-rag-mcp --version "${version}" --output json \
  >reports/agentic/registry-mcp.json
for name in recsys-context-agent recsys-context-agent-sandbox; do
  arctl agent show "${name}" --output json \
    >"reports/agentic/registry-${name}.json"
done
python3 - "${version}" "${commit}" reports/agentic/registry-*.json <<'PY'
import json
import sys

version, commit, *paths = sys.argv[1:]
for path in paths:
    payload = json.load(open(path, encoding="utf-8"))
    serialized = json.dumps(payload, sort_keys=True)
    assert version in serialized, (path, version)
    assert commit in serialized, (path, commit)
PY
echo "Agent Registry contains MCP, regular Agent, and SandboxAgent at ${version}."
