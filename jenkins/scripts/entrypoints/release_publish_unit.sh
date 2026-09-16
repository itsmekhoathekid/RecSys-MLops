#!/usr/bin/env bash
set -euo pipefail

unit_name="${1:?publish unit is required}"
plan_path="${2:-.ci-release-plan.json}"
[[ -s "${plan_path}" ]] || {
  printf 'release plan does not exist: %s\n' "${plan_path}" >&2
  exit 2
}

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/runtime.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/deploy/agentic/rotation.sh
source jenkins/scripts/deploy/agentic/registry.sh
trap agentic_registry_close_tunnel EXIT

[[ -s .ci-deploy/publish-preflight-commit ]] \
  && [[ "$(<.ci-deploy/publish-preflight-commit)" == "$(git rev-parse HEAD)" ]] || {
    recsys_error "Agent Registry publication preflight is missing or stale"
    exit 2
  }

unit_action=""
unit_registry_artifact=""
while IFS=$'\t' read -r record_type value_a _value_b _value_c _value_d; do
  case "${record_type}" in
    ACTION) unit_action="${value_a}" ;;
    REGISTRY_ARTIFACT) unit_registry_artifact="${value_a}" ;;
    UNIT|IMAGE|SELECTED_COMPONENT) ;;
    *)
      recsys_error "unsupported publish context record: ${record_type}"
      exit 2
      ;;
  esac
done < <(
  python3 jenkins/python/release_plan.py deploy-context \
    "${unit_name}" --plan "${plan_path}"
)

[[ "${unit_action}" == "agent-registry-publish" \
  && -n "${unit_registry_artifact}" ]] || {
  recsys_error "publish unit ${unit_name} is not an Agent Registry artifact"
  exit 2
}
publish_agent_registry_artifact "${unit_registry_artifact}"
