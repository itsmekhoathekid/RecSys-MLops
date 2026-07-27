#!/usr/bin/env bash

build_record_image() {
  image_manifest_record "${BUILD_MANIFEST_PATH}" "$1" "$2"
}

build_scan_image() {
  local image="$1"
  local image_name="$2"
  if ! recsys_is_true "${BUILD_SCAN_ENABLED}"; then
    recsys_log "skip vulnerability scan for ${image}; CONTAINER_SCAN_ENABLED=${BUILD_SCAN_ENABLED}"
    return 0
  fi

  local args=(image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL "${image}")
  if command -v trivy >/dev/null 2>&1; then
    trivy "${args[@]}"
    return
  fi

  local archive="${BUILD_MANIFEST_DIR}/${image_name}-${BUILD_IMAGE_TAG}.tar"
  local scan_container="trivy-${image_name}-${BUILD_NUMBER:-$$}"
  scan_container="${scan_container//[^a-zA-Z0-9_.-]/-}"
  docker save --output "${archive}" "${image}"
  docker rm -f "${scan_container}" >/dev/null 2>&1 || true
  docker create --name "${scan_container}" aquasec/trivy:0.58.2 \
    image --exit-code 1 --ignore-unfixed --severity HIGH,CRITICAL \
    --input /image.tar >/dev/null
  docker cp "${archive}" "${scan_container}:/image.tar"
  local scan_status=0
  docker start -a "${scan_container}" || scan_status=$?
  docker rm -f "${scan_container}" >/dev/null 2>&1 || true
  rm -f "${archive}"
  return "${scan_status}"
}

build_refresh_registry_login() {
  if ! recsys_is_true "${BUILD_PUBLISH_IMAGES}" || [[ "${BUILD_REGISTRY_HOST}" != *".pkg.dev" ]]; then
    return 0
  fi
  registry_login_gcp "${BUILD_IMAGE_REGISTRY}" >/dev/null
  recsys_log "refreshed Docker login for ${BUILD_REGISTRY_HOST}"
}

build_image() {
  local name="$1"
  local dockerfile="$2"
  shift 2
  local local_image="${name}:${BUILD_IMAGE_TAG}"
  local remote_image="${BUILD_IMAGE_REGISTRY}/${name}:${BUILD_IMAGE_TAG}"
  local image_key
  local digest=""
  local digest_hash=""
  local push_log=""

  docker build --platform "${BUILD_DOCKER_PLATFORM}" "$@" \
    -f "${dockerfile}" \
    -t "${local_image}" .
  docker tag "${local_image}" "${remote_image}"
  image_key="$(image_manifest_key "${name}")"
  build_record_image "${image_key}_IMAGE" "${remote_image}"

  build_scan_image "${local_image}" "${name}"

  if recsys_is_true "${BUILD_PUBLISH_IMAGES}"; then
    build_refresh_registry_login
    push_log="${BUILD_MANIFEST_DIR}/push-${name}-${BUILD_IMAGE_TAG}.log"
    docker push "${remote_image}" | tee "${push_log}"
    digest_hash="$(awk '/digest: sha256:/ {print $2}' "${push_log}" | tail -n 1)"
    digest="$(
      docker image inspect "${remote_image}" --format '{{join .RepoDigests " "}}' \
        | tr ' ' '\n' \
        | grep -F "${BUILD_IMAGE_REGISTRY}/${name}@" \
        | head -n 1 || true
    )"
    if [[ -z "${digest}" && "${digest_hash}" == sha256:* ]]; then
      digest="${BUILD_IMAGE_REGISTRY}/${name}@${digest_hash}"
    fi
    rm -f "${push_log}"
    [[ -n "${digest}" ]] || {
      recsys_error "push completed but immutable digest was not resolved for ${remote_image}"
      return 1
    }
    docker pull "${digest}" >/dev/null
    build_record_image "${image_key}_DIGEST" "${digest}"
  else
    recsys_log "skip docker push for ${remote_image}; PUBLISH_IMAGES=${BUILD_PUBLISH_IMAGES}"
  fi
}

build_ensure_base_python() {
  if [[ "${BUILD_BASE_PYTHON_DONE}" == "0" ]]; then
    build_image "recsys-base-python" "infra/docker/Dockerfile.base-python"
    BUILD_BASE_PYTHON_DONE=1
  fi
}

build_ensure_spark_base() {
  if [[ "${BUILD_SPARK_BASE_DONE}" == "0" ]]; then
    build_image "recsys-spark" "apps/data-platform/Dockerfile.spark"
    BUILD_SPARK_BASE_DONE=1
  fi
}
