#!/usr/bin/env bash
set -euo pipefail

snapshot_path=".ci-deploy/production-snapshot.json"
[[ -s "${snapshot_path}" ]] || {
  printf '[ROLLBACK] production snapshot is missing; refusing blind rollback.\n' >&2
  exit 2
}
python3 -m jenkins.python.deployment_transaction rollback \
  --snapshot "${snapshot_path}" \
  --output .ci-deploy/rollback-evidence.json
