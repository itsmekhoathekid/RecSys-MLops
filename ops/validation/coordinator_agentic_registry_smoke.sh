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

for spec in \
  "agent recsys/recsys-coordinator-agent coordinator" \
  "agent recsys/recsys-context-agent-sandbox context-agent" \
  "agent recsys/recsys-recommendation-agent-sandbox recommendation-agent" \
  "mcp recsys/recsys-feature-rag-mcp context-mcp" \
  "mcp recsys/recsys-recommendation-mcp recommendation-mcp"; do
  read -r kind name output_name <<<"${spec}"
  arctl get "${kind}" "${name}" --tag "${tag}" -o json \
    >"reports/agentic/registry-${output_name}.json"
done

python3 - "${version}" "${tag}" "${commit}" \
  reports/agentic/registry-coordinator.json \
  reports/agentic/registry-context-agent.json \
  reports/agentic/registry-recommendation-agent.json \
  reports/agentic/registry-context-mcp.json \
  reports/agentic/registry-recommendation-mcp.json <<'PY'
import json
import sys

version, tag, commit, coordinator_path, *dependency_paths = sys.argv[1:]
for path in [coordinator_path, *dependency_paths]:
    payload = json.load(open(path, encoding="utf-8"))
    serialized = json.dumps(payload, sort_keys=True)
    assert version in serialized, (path, version)
    assert commit in serialized, (path, commit)

coordinator = json.load(open(coordinator_path, encoding="utf-8"))
assert [item["name"] for item in coordinator["spec"]["mcpServers"]] == [
    "recsys-feature-rag-mcp",
    "recsys-recommendation-mcp",
]
assert coordinator["metadata"]["annotations"]["recsys.dev/a2a-dependencies"] == (
    f"recsys/recsys-context-agent-sandbox@{tag},"
    f"recsys/recsys-recommendation-agent-sandbox@{tag}"
)
PY

echo "Agent Registry contains the coordinator and all four matching dependencies at ${version}."
