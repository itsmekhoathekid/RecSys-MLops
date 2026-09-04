#!/usr/bin/env bash
set -euo pipefail

plan_path="${1:-.ci-release-plan.json}"
[[ -f "${plan_path}" ]] || {
  printf 'release plan does not exist: %s\n' "${plan_path}" >&2
  exit 2
}

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/image_manifest.sh

image_registry="${IMAGE_PUSH_REGISTRY:-${IMAGE_REGISTRY:-$(python3 jenkins/python/configuration.py gcp imageRegistry)}}"
image_registry="${image_registry%/}"
image_tag="${IMAGE_TAG:-${GIT_COMMIT:-$(git rev-parse HEAD)}}"

release_image_reference() {
  local image_name="$1"
  local reference
  reference="$(image_manifest_lookup "${image_name}")"
  printf '%s' "${reference:-${image_registry}/${image_name}:${image_tag}}"
}

while IFS= read -r artifact; do
  [[ -n "${artifact}" ]] || continue
  case "${artifact}" in
    kubeflow-bst)
      training_image="$(release_image_reference recsys-mlops-training)"
      RECSYS_PIPELINE_IMAGE="${training_image}" \
        RECSYS_RAY_IMAGE="${training_image}" \
        RECSYS_SPARK_ML_IMAGE="$(release_image_reference recsys-spark-ml)" \
        bash jenkins/scripts/build/kfp_package.sh
      ;;
    *)
      recsys_error "unsupported release-plan artifact: ${artifact}"
      exit 2
      ;;
  esac
done < <(
  python3 jenkins/python/release_plan.py plan-artifacts --plan "${plan_path}"
)
