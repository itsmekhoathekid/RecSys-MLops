#!/usr/bin/env bash
set -euo pipefail

plan_path="${1:-.ci-release-plan.json}"
[[ -s "${plan_path}" ]] || {
  printf 'release plan does not exist: %s\n' "${plan_path}" >&2
  exit 2
}

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/deploy/preflight/gcp.sh

branch_name="${BRANCH_NAME:-${GIT_BRANCH:-}}"
checked_out_main=0
if git rev-parse --verify origin/main >/dev/null 2>&1 \
  && [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]]; then
  checked_out_main=1
fi
if [[ "${branch_name}" != "main" && "${branch_name}" != "origin/main" ]] \
  && [[ "${checked_out_main}" != "1" ]] \
  && ! recsys_is_true "${DEPLOY_PULL_REQUESTS:-0}" \
  && ! recsys_is_true "${FORCE_DEPLOY:-0}"; then
  recsys_error "GCP production deploy requires main, DEPLOY_PULL_REQUESTS=true, or FORCE_DEPLOY=true"
  exit 2
fi
recsys_is_true "${PUBLISH_IMAGES:-0}" || {
  recsys_error "GCP production deploy requires PUBLISH_IMAGES=true"
  exit 2
}
verify_gcp_release_target "${plan_path}"
mkdir -p .ci-deploy
git rev-parse HEAD >.ci-deploy/preflight-commit
