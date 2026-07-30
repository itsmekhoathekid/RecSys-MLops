#!/usr/bin/env bash
set -euo pipefail

jenkins_url="${JENKINS_URL:?JENKINS_URL is required}"
jenkins_user="${JENKINS_USER:?JENKINS_USER is required}"
jenkins_token="${JENKINS_TOKEN:?JENKINS_TOKEN is required}"
jenkins_job="${JENKINS_JOB:-RecSys-GitHub-CICD}"
max_parallel="${COMPONENT_CI_MAX_PARALLEL:-3}"
coverage_min="${COVERAGE_MIN:-90}"
gateway_credentials_id="${GATEWAY_SMOKE_CREDENTIALS_ID:-}"
promotion_manifest_uri="${PROMOTION_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/latest.json}"
components="${FORCE_COMPONENTS:-materialize,training,dp1,dp2,dp3,api,kserve,rollout,drift,stream_offline,stream_online,analytics,demo_web,ci_config}"
crumb_header=()

jenkins_url="${jenkins_url%/}"
if crumb_json="$(
  curl -fsS --user "${jenkins_user}:${jenkins_token}" \
    "${jenkins_url}/crumbIssuer/api/json" 2>/dev/null
)"; then
  crumb_field="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["crumbRequestField"])' \
      <<<"${crumb_json}"
  )"
  crumb_value="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["crumb"])' \
      <<<"${crumb_json}"
  )"
  crumb_header=(-H "${crumb_field}: ${crumb_value}")
fi

headers_file="$(mktemp)"
trap 'rm -f "${headers_file}"' EXIT
curl -fsS \
  --user "${jenkins_user}:${jenkins_token}" \
  "${crumb_header[@]}" \
  -D "${headers_file}" \
  -o /dev/null \
  -X POST "${jenkins_url}/job/${jenkins_job}/buildWithParameters" \
  --data-urlencode "PUBLISH_IMAGES=true" \
  --data-urlencode "FORCE_DEPLOY=true" \
  --data-urlencode "COMPONENT_CI_MAX_PARALLEL=${max_parallel}" \
  --data-urlencode "COVERAGE_MIN=${coverage_min}" \
  --data-urlencode "GATEWAY_SMOKE_CREDENTIALS_ID=${gateway_credentials_id}" \
  --data-urlencode "PROMOTION_MANIFEST_URI=${promotion_manifest_uri}" \
  --data-urlencode "FORCE_COMPONENTS=${components}"

queue_url="$(
  awk 'tolower($1) == "location:" {gsub(/\r/, "", $2); print $2}' \
    "${headers_file}" | tail -n 1
)"
printf 'Full Jenkins CI/CD queued%s\n' \
  "${queue_url:+: ${queue_url}}"
