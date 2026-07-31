#!/usr/bin/env bash

initialize_release_build() {
  BUILD_IMAGE_REGISTRY="${IMAGE_PUSH_REGISTRY:-${IMAGE_REGISTRY:-$(python3 jenkins/python/configuration.py gcp imageRegistry)}}"
  BUILD_IMAGE_REGISTRY="${BUILD_IMAGE_REGISTRY%/}"
  BUILD_REGISTRY_HOST="${BUILD_IMAGE_REGISTRY%%/*}"
  BUILD_IMAGE_TAG="${IMAGE_TAG:-${GIT_COMMIT:-}}"
  BUILD_PUBLISH_IMAGES="${PUBLISH_IMAGES:-1}"
  BUILD_REQUIRE_GCP="${REQUIRE_GCP_ARTIFACT_REGISTRY:-1}"
  BUILD_MANIFEST_DIR="${IMAGE_MANIFEST_DIR:-.ci-image-manifest}"
  BUILD_DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
  BUILD_SCAN_ENABLED="${CONTAINER_SCAN_ENABLED:-1}"
  BUILD_SCAN_REPORT_DIR="${REPORTS_DIR:-reports}/validation/container-scans"
  BUILD_REGISTRY_LOGIN_EPOCH="$(date +%s)"

  if [[ -z "${BUILD_IMAGE_TAG}" ]]; then
    BUILD_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
  fi

  if recsys_is_true "${BUILD_REQUIRE_GCP}"; then
    load_gcp_production_config
    registry_validate_gcp_repository \
      "${BUILD_IMAGE_REGISTRY}" \
      "$(gcp_production_field imageRegistry)"
    recsys_is_true "${BUILD_PUBLISH_IMAGES}" || {
      recsys_error "PUBLISH_IMAGES must be true for the production Artifact Registry"
      return 2
    }
    [[ "${BUILD_IMAGE_TAG}" =~ ^[0-9a-fA-F]{40}$ ]] || {
      recsys_error "production image tag must be the full 40-character GIT_COMMIT; got ${BUILD_IMAGE_TAG}"
      return 2
    }
  fi

  mkdir -p "${BUILD_MANIFEST_DIR}" "${BUILD_SCAN_REPORT_DIR}"
  BUILD_MANIFEST_PATH="${BUILD_MANIFEST_DIR}/release-plan.env"
  : >"${BUILD_MANIFEST_PATH}"
  export BUILD_IMAGE_REGISTRY BUILD_REGISTRY_HOST BUILD_IMAGE_TAG
  export BUILD_PUBLISH_IMAGES BUILD_MANIFEST_DIR BUILD_MANIFEST_PATH
  export BUILD_DOCKER_PLATFORM BUILD_SCAN_ENABLED BUILD_SCAN_REPORT_DIR
  export BUILD_REGISTRY_LOGIN_EPOCH
}
