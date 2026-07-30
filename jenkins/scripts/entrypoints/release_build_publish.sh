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

build_runtime_initialize release-plan

while IFS= read -r image_name; do
  [[ -n "${image_name}" ]] || continue
  build_image "${image_name}"
done < <(
  python3 -c '
import json, sys
for item in json.load(open(sys.argv[1], encoding="utf-8"))["buildImages"]:
    print(item)
' "${plan_path}"
)

if python3 -c '
import json, sys
raise SystemExit(
    0 if "recsys-spark" in json.load(open(sys.argv[1], encoding="utf-8"))["buildImages"] else 1
)
' "${plan_path}"; then
  bash jenkins/scripts/test/unified_spark_image.sh \
    "recsys-spark:${BUILD_IMAGE_TAG}"
fi

while IFS= read -r artifact; do
  [[ -n "${artifact}" ]] || continue
  case "${artifact}" in
    kubeflow-bst)
      RECSYS_PIPELINE_IMAGE="${BUILD_IMAGE_REGISTRY}/recsys-mlops-training:${BUILD_IMAGE_TAG}" \
        RECSYS_RAY_IMAGE="${BUILD_IMAGE_REGISTRY}/recsys-mlops-training:${BUILD_IMAGE_TAG}" \
        RECSYS_SPARK_IMAGE="${BUILD_IMAGE_REGISTRY}/recsys-spark:${BUILD_IMAGE_TAG}" \
        bash jenkins/scripts/build/kfp_package.sh
      ;;
    *)
      recsys_error "unsupported release-plan artifact: ${artifact}"
      exit 2
      ;;
  esac
done < <(
  python3 -c '
import json, sys
for item in json.load(open(sys.argv[1], encoding="utf-8"))["buildArtifacts"]:
    print(item)
' "${plan_path}"
)

recsys_log "wrote release image manifest: ${BUILD_MANIFEST_PATH}"
