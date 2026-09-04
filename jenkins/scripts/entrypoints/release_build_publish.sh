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
spark_profiles=()
image_index=0
image_total="$(
  python3 jenkins/python/release_plan.py plan-images --plan "${plan_path}" \
    | awk 'NF {count++} END {print count+0}'
)"

while IFS= read -r image_name; do
  [[ -n "${image_name}" ]] || continue
  ((image_index += 1))
  recsys_log "[BUILD] Build image ${image_index}/${image_total}: ${image_name}"
  build_publish_image "${image_name}"
  case "${image_name}" in
    recsys-spark-data) spark_profiles+=(data) ;;
    recsys-spark-analytics) spark_profiles+=(analytics) ;;
    recsys-spark-ml) spark_profiles+=(ml) ;;
  esac
done < <(
  python3 jenkins/python/release_plan.py plan-images --plan "${plan_path}"
)

for profile in "${spark_profiles[@]}"; do
  bash jenkins/scripts/test/spark_image.sh \
    "recsys-spark-${profile}:${BUILD_IMAGE_TAG}" "${profile}"
done

recsys_log "wrote release image manifest: ${BUILD_MANIFEST_PATH}"
