#!/usr/bin/env bash
set -euo pipefail

plan_path="${1:-.ci-release-plan.json}"
[[ -f "${plan_path}" ]] || {
  printf 'release plan does not exist: %s\n' "${plan_path}" >&2
  exit 2
}

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/config.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/lib/registry.sh
source jenkins/scripts/build/runtime.sh
source jenkins/scripts/build/engine.sh

initialize_release_build
built_spark=0
image_index=0
image_total="$(
  python3 jenkins/python/release_plan.py plan-images --plan "${plan_path}" \
    | awk 'NF {count++} END {print count+0}'
)"

while IFS= read -r image_name; do
  [[ -n "${image_name}" ]] || continue
  ((image_index += 1))
  recsys_log "[BUILD] Build image ${image_index}/${image_total}: ${image_name}"
  build_scan_publish_image "${image_name}"
  if [[ "${image_name}" == "recsys-spark" ]]; then
    built_spark=1
  fi
done < <(
  python3 jenkins/python/release_plan.py plan-images --plan "${plan_path}"
)

if [[ "${built_spark}" == "1" ]]; then
  bash jenkins/scripts/test/unified_spark_image.sh \
    "recsys-spark:${BUILD_IMAGE_TAG}"
fi

recsys_log "wrote release image manifest: ${BUILD_MANIFEST_PATH}"
