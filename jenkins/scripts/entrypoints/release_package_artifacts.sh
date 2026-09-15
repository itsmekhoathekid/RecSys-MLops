#!/usr/bin/env bash
set -euo pipefail

plan_path="${1:-.ci-release-plan.json}"
[[ -f "${plan_path}" ]] || {
  printf 'release plan does not exist: %s\n' "${plan_path}" >&2
  exit 2
}

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/lib/registry.sh
source jenkins/scripts/build/helm_oci.sh

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
  artifact_kind=""
  artifact_chart=""
  artifact_repository=""
  while IFS=$'\t' read -r record_type value_a value_b value_c; do
    [[ "${record_type}" == "ARTIFACT" ]] || {
      recsys_error "unsupported artifact context record: ${record_type}"
      exit 2
    }
    artifact_kind="${value_a}"
    artifact_chart="${value_b}"
    artifact_repository="${value_c}"
  done < <(
    python3 jenkins/python/release_plan.py artifact-context "${artifact}"
  )
  case "${artifact_kind}" in
    kfp-package)
      training_image="$(release_image_reference recsys-mlops-training)"
      RECSYS_PIPELINE_IMAGE="${training_image}" \
        RECSYS_RAY_IMAGE="${training_image}" \
        RECSYS_SPARK_ML_IMAGE="$(release_image_reference recsys-spark-ml)" \
        bash jenkins/scripts/build/kfp_package.sh
      ;;
    helm-oci)
      package_publish_helm_oci \
        "${artifact}" \
        "${artifact_chart}" \
        "${artifact_repository}" \
        "${image_registry}" \
        "${image_tag}" \
        "${PUBLISH_IMAGES:-0}"
      ;;
    *)
      recsys_error "unsupported release-plan artifact kind: ${artifact_kind}"
      exit 2
      ;;
  esac
done < <(
  python3 jenkins/python/release_plan.py plan-artifacts --plan "${plan_path}"
)
