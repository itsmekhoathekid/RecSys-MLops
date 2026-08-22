#!/usr/bin/env bash
set -euo pipefail

commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
version="0.1.0-${commit:0:12}"

for spec in \
  "mcp recsys/recsys-recommendation-mcp" \
  "agent recsys/recsys-recommendation-agent-sandbox"; do
  read -r kind name <<<"${spec}"
  payload="$(arctl get "${kind}" "${name}" --tag "${version}" -o json)"
  grep -Fq "${commit}" <<<"${payload}"
  printf 'PASS: %s %s@%s matches %s\n' "${kind}" "${name}" "${version}" "${commit}"
done
