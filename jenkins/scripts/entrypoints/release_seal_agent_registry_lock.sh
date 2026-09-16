#!/usr/bin/env bash
set -euo pipefail

plan_path="${1:-.ci-release-plan.json}"
[[ -s "${plan_path}" ]] || {
  printf 'release plan does not exist: %s\n' "${plan_path}" >&2
  exit 2
}

publish_count="$(
  python3 jenkins/python/release_plan.py plan-units \
    --plan "${plan_path}" --phase publish | awk 'NF {count++} END {print count+0}'
)"
if [[ "${publish_count}" == "0" ]]; then
  printf '[PUBLISH] release has no Agent Registry artifacts; lock is not required\n'
  exit 0
fi

python3 -m jenkins.python.agent_registry_release seal-lock \
  --plan "${plan_path}" \
  --manifest-dir .ci-deploy/agent-registry-manifests \
  --readback-dir .ci-deploy/agent-registry-readbacks \
  --output .ci-deploy/agent-registry-lock.json
printf '[PUBLISH] sealed Agent Registry deployment lock\n'
