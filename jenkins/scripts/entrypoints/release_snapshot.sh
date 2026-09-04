#!/usr/bin/env bash
set -euo pipefail

plan_path="${1:-.ci-release-plan.json}"
mkdir -p .ci-deploy
python3 -m jenkins.python.deployment_transaction snapshot \
  --plan "${plan_path}" \
  --output .ci-deploy/production-snapshot.json
