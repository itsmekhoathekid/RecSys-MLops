#!/usr/bin/env bash

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/deploy/preflight/gcp.sh

release_has_agent_registry_units() {
  local plan_path="$1"
  local publish_units
  publish_units="$(
    python3 jenkins/python/release_plan.py plan-units \
      --plan "${plan_path}" --phase publish
  )"
  grep -Fq $'agent-registry:catalog' <<<"${publish_units}"
}

release_assert_authorized_source() {
  local branch_name="${BRANCH_NAME:-${GIT_BRANCH:-}}"
  local checked_out_main=0
  if git rev-parse --verify origin/main >/dev/null 2>&1 \
    && [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]]; then
    checked_out_main=1
  fi
  if [[ "${branch_name}" != "main" && "${branch_name}" != "origin/main" ]] \
    && [[ "${checked_out_main}" != "1" ]] \
    && ! recsys_is_true "${DEPLOY_PULL_REQUESTS:-0}" \
    && ! recsys_is_true "${FORCE_DEPLOY:-0}"; then
    recsys_error "GCP production release requires main or an explicit override"
    return 2
  fi
}

release_verify_publish_inputs() {
  local plan_path="$1"
  release_assert_authorized_source
  recsys_is_true "${PUBLISH_IMAGES:-0}" || {
    recsys_error "GCP production release requires PUBLISH_IMAGES=true"
    return 2
  }
  mkdir -p .ci-deploy
  verify_gcp_release_target "${plan_path}"
}

release_validate_agent_registry_lock() {
  local plan_path="$1"
  local commit
  commit="$(git rev-parse HEAD)"
  [[ -s .ci-deploy/publish-preflight-commit ]] \
    && [[ "$(<.ci-deploy/publish-preflight-commit)" == "${commit}" ]] || {
      recsys_error "Agent Registry publication preflight is missing or stale"
      return 2
    }
  [[ -s .ci-deploy/agent-registry-lock.json ]] || {
    recsys_error "Agent Registry deployment lock is missing"
    return 2
  }
  python3 -m jenkins.python.agent_registry_release validate-lock \
    --plan "${plan_path}" \
    --lock .ci-deploy/agent-registry-lock.json \
    --manifest-dir .ci-deploy/agent-registry-manifests \
    --readback-dir .ci-deploy/agent-registry-readbacks
}
