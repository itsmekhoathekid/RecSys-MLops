#!/usr/bin/env bash

build_record_image() {
  image_manifest_record "${BUILD_MANIFEST_PATH}" "$1" "$2"
}

build_reuse_shared_image() {
  local name="$1"
  local image_key="$2"
  local local_image="$3"
  local shared_manifest="$4"
  local remote_image=""
  local digest=""

  [[ -s "${shared_manifest}" ]] || return 1
  remote_image="$(
    awk -F= -v key="${image_key}_IMAGE" \
      '$1 == key {sub(/^[^=]*=/, "", $0); print; exit}' "${shared_manifest}"
  )"
  digest="$(
    awk -F= -v key="${image_key}_DIGEST" \
      '$1 == key {sub(/^[^=]*=/, "", $0); print; exit}' "${shared_manifest}"
  )"
  [[ -n "${remote_image}" ]] || return 1
  if recsys_is_true "${BUILD_PUBLISH_IMAGES}"; then
    [[ "${digest}" == "${BUILD_IMAGE_REGISTRY}/${name}@"sha256:* ]] || return 1
  fi

  if ! docker image inspect "${local_image}" >/dev/null 2>&1; then
    [[ -n "${digest}" ]] || return 1
    docker pull "${digest}" >/dev/null
    docker tag "${digest}" "${local_image}"
  fi
  build_record_image "${image_key}_IMAGE" "${remote_image}"
  if [[ -n "${digest}" ]]; then
    build_record_image "${image_key}_DIGEST" "${digest}"
  fi
  recsys_log "reused build result for ${name}:${BUILD_IMAGE_TAG}"
}

build_publish_shared_manifest() {
  local image_key="$1"
  local shared_manifest="$2"
  local temporary_manifest="${shared_manifest}.tmp.${BUILD_COMPONENT}.$$"

  awk -F= -v image_key="${image_key}_IMAGE" -v digest_key="${image_key}_DIGEST" \
    '$1 == image_key || $1 == digest_key' \
    "${BUILD_MANIFEST_PATH}" >"${temporary_manifest}"
  [[ -s "${temporary_manifest}" ]]
  mv -f "${temporary_manifest}" "${shared_manifest}"
}

build_scan_image() {
  local image="$1"
  local image_name="$2"
  local scan_report="${BUILD_SCAN_REPORT_DIR}/${image_name}.json"
  if ! recsys_is_true "${BUILD_SCAN_ENABLED}"; then
    recsys_log "skip vulnerability scan for ${image}; CONTAINER_SCAN_ENABLED=${BUILD_SCAN_ENABLED}"
    return 0
  fi

  local args=(image --exit-code 0 --ignore-unfixed --scanners vuln --format json "${image}")
  if command -v trivy >/dev/null 2>&1; then
    trivy "${args[@]}" >"${scan_report}"
    python3 jenkins/python/container_scan_policy.py \
      --image-name "${image_name}" \
      --report "${scan_report}"
    return
  fi

  local archive="${BUILD_MANIFEST_DIR}/${image_name}-${BUILD_IMAGE_TAG}-${BUILD_COMPONENT}-$$.tar"
  local scan_container="trivy-${image_name}-${BUILD_NUMBER:-manual}-${BUILD_COMPONENT}-$$"
  scan_container="${scan_container//[^a-zA-Z0-9_.-]/-}"
  docker save --output "${archive}" "${image}"
  docker rm -f "${scan_container}" >/dev/null 2>&1 || true
  docker create --name "${scan_container}" aquasec/trivy:0.58.2 \
    image --exit-code 0 --ignore-unfixed --scanners vuln --format json \
    --output /scan.json --input /image.tar >/dev/null
  docker cp "${archive}" "${scan_container}:/image.tar"
  local scan_status=0
  docker start -a "${scan_container}" || scan_status=$?
  if [[ "${scan_status}" -eq 0 ]]; then
    docker cp "${scan_container}:/scan.json" "${scan_report}"
    python3 jenkins/python/container_scan_policy.py \
      --image-name "${image_name}" \
      --report "${scan_report}" || scan_status=$?
  fi
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

build_image_locked() {
  local name="$1"
  local dockerfile="$2"
  shift 2
  local local_image="${name}:${BUILD_IMAGE_TAG}"
  local remote_image="${BUILD_IMAGE_REGISTRY}/${name}:${BUILD_IMAGE_TAG}"
  local image_key
  local shared_manifest
  local digest=""
  local digest_hash=""
  local push_log=""

  image_key="$(image_manifest_key "${name}")"
  shared_manifest="${BUILD_SHARED_MANIFEST_DIR}/${name}.env"
  if build_reuse_shared_image \
    "${name}" "${image_key}" "${local_image}" "${shared_manifest}"; then
    return 0
  fi

  docker build --platform "${BUILD_DOCKER_PLATFORM}" "$@" \
    -f "${dockerfile}" \
    -t "${local_image}" .
  docker tag "${local_image}" "${remote_image}"
  build_record_image "${image_key}_IMAGE" "${remote_image}"

  build_scan_image "${local_image}" "${name}"

  if recsys_is_true "${BUILD_PUBLISH_IMAGES}"; then
    build_refresh_registry_login
    push_log="${BUILD_MANIFEST_DIR}/push-${name}-${BUILD_IMAGE_TAG}-${BUILD_COMPONENT}-$$.log"
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
  build_publish_shared_manifest "${image_key}" "${shared_manifest}"
}

build_image() {
  local name="$1"
  local lock_root="${BUILD_LOCK_ROOT:-${JENKINS_HOME:-.ci-build-locks}/ci-build-locks}"
  local lock_path
  local failure_path="${BUILD_SHARED_MANIFEST_DIR}/${name}.failed"

  mkdir -p "${lock_root}" "${BUILD_SHARED_MANIFEST_DIR}" "${BUILD_SCAN_REPORT_DIR}"
  lock_path="${lock_root}/$(recsys_slug "${name}-${BUILD_IMAGE_TAG}").lock"
  if command -v flock >/dev/null 2>&1; then
    (
      flock -w "${BUILD_LOCK_TIMEOUT_SECONDS:-3600}" 9
      if [[ -s "${failure_path}" ]]; then
        recsys_error "shared image ${name}:${BUILD_IMAGE_TAG} already failed in this build"
        cat "${failure_path}" >&2
        exit 1
      fi
      set +e
      (
        set -e
        build_image_locked "$@"
      )
      build_status=$?
      set -e
      if [[ "${build_status}" -ne 0 ]]; then
        printf 'component=%s status=%s report=%s\n' \
          "${BUILD_COMPONENT}" "${build_status}" "${BUILD_SCAN_REPORT_DIR}/${name}.log" \
          >"${failure_path}"
        exit "${build_status}"
      fi
      rm -f "${failure_path}"
    ) 9>"${lock_path}"
    return
  fi
  if recsys_is_true "${BUILD_REQUIRE_GCP}"; then
    recsys_error "flock is required for production image build deduplication"
    return 2
  fi
  build_image_locked "$@"
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
