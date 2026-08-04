#!/usr/bin/env bash

record_built_image() {
  image_manifest_record "${BUILD_MANIFEST_PATH}" "$1" "$2"
}

scan_built_image() {
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

  local archive="${BUILD_MANIFEST_DIR}/${image_name}-${BUILD_IMAGE_TAG}-$$.tar"
  local scan_container="trivy-${image_name}-${BUILD_NUMBER:-manual}-$$"
  local scan_cache_volume="${TRIVY_CACHE_VOLUME:-recsys-trivy-cache}"
  local scan_status=0
  scan_container="${scan_container//[^a-zA-Z0-9_.-]/-}"

  docker volume create "${scan_cache_volume}" >/dev/null || scan_status=$?
  docker rm -f "${scan_container}" >/dev/null 2>&1 || true
  if [[ "${scan_status}" -eq 0 ]]; then
    docker save --output "${archive}" "${image}" || scan_status=$?
  fi
  if [[ "${scan_status}" -eq 0 ]]; then
    docker create \
      --name "${scan_container}" \
      --mount "type=volume,source=${scan_cache_volume},target=/root/.cache" \
      aquasec/trivy:0.58.2 \
      image --exit-code 0 --ignore-unfixed --scanners vuln --format json \
      --output /scan.json --input /image.tar >/dev/null || scan_status=$?
  fi
  if [[ "${scan_status}" -eq 0 ]]; then
    docker cp "${archive}" "${scan_container}:/image.tar" || scan_status=$?
  fi
  if [[ "${scan_status}" -eq 0 ]]; then
    docker start -a "${scan_container}" || scan_status=$?
  fi
  if [[ "${scan_status}" -eq 0 ]]; then
    docker cp "${scan_container}:/scan.json" "${scan_report}" || scan_status=$?
  fi
  if [[ "${scan_status}" -eq 0 ]]; then
    python3 jenkins/python/container_scan_policy.py \
      --image-name "${image_name}" \
      --report "${scan_report}" || scan_status=$?
  fi
  docker rm -f "${scan_container}" >/dev/null 2>&1 || true
  rm -f "${archive}"
  return "${scan_status}"
}

refresh_registry_login_if_needed() {
  if ! recsys_is_true "${BUILD_PUBLISH_IMAGES}" || [[ "${BUILD_REGISTRY_HOST}" != *".pkg.dev" ]]; then
    return 0
  fi
  local now
  now="$(date +%s)"
  if ((now - BUILD_REGISTRY_LOGIN_EPOCH < ${REGISTRY_LOGIN_REFRESH_SECONDS:-300})); then
    return 0
  fi
  registry_login_gcp "${BUILD_IMAGE_REGISTRY}" >/dev/null
  BUILD_REGISTRY_LOGIN_EPOCH="${now}"
  export BUILD_REGISTRY_LOGIN_EPOCH
  recsys_log "refreshed Docker login for ${BUILD_REGISTRY_HOST}"
}

push_built_image() {
  local remote_image="$1"
  local push_log="$2"

  if docker push "${remote_image}" 2>&1 | tee "${push_log}"; then
    return 0
  fi
  if [[ "${BUILD_REGISTRY_HOST}" != *".pkg.dev" ]]; then
    return 1
  fi

  recsys_log "Docker push failed for ${remote_image}; refreshing Artifact Registry login and retrying once"
  registry_login_gcp "${BUILD_IMAGE_REGISTRY}" >/dev/null
  BUILD_REGISTRY_LOGIN_EPOCH="$(date +%s)"
  export BUILD_REGISTRY_LOGIN_EPOCH
  docker push "${remote_image}" 2>&1 | tee "${push_log}"
}

build_scan_publish_image() {
  local name="$1"
  local record_type value_a value_b
  local dockerfile=""
  local context=""
  local docker_args=(--platform "${BUILD_DOCKER_PLATFORM}")
  local local_image="${name}:${BUILD_IMAGE_TAG}"
  local remote_image="${BUILD_IMAGE_REGISTRY}/${name}:${BUILD_IMAGE_TAG}"
  local image_key
  local digest=""
  local digest_hash=""
  local push_log=""

  while IFS=$'\t' read -r record_type value_a value_b; do
    case "${record_type}" in
      CONTEXT)
        dockerfile="${value_a}"
        context="${value_b}"
        ;;
      ARG)
        docker_args+=(--build-arg "${value_a}")
        ;;
      *)
        recsys_error "unsupported image catalog build record: ${record_type}"
        return 2
        ;;
    esac
  done < <(
    python3 jenkins/python/image_catalog.py build-spec "${name}" --tag "${BUILD_IMAGE_TAG}"
  )
  [[ -n "${dockerfile}" && -n "${context}" ]] || {
    recsys_error "catalog build context is incomplete for ${name}"
    return 2
  }

  docker build "${docker_args[@]}" -f "${dockerfile}" -t "${local_image}" "${context}"
  if [[ "${name}" == "recsys-online-feature-api" || "${name}" == "recsys-inference-api" ]]; then
    bash jenkins/scripts/test/serving_images.sh "${local_image}" "${name}"
  fi
  docker tag "${local_image}" "${remote_image}"
  image_key="$(image_manifest_key "${name}")"
  record_built_image "${image_key}_LOCAL_IMAGE" "${local_image}"
  record_built_image "${image_key}_IMAGE" "${remote_image}"
  scan_built_image "${local_image}" "${name}"

  if ! recsys_is_true "${BUILD_PUBLISH_IMAGES}"; then
    recsys_log "skip docker push for ${remote_image}; PUBLISH_IMAGES=${BUILD_PUBLISH_IMAGES}"
    return 0
  fi

  refresh_registry_login_if_needed
  push_log="${BUILD_MANIFEST_DIR}/push-${name}-${BUILD_IMAGE_TAG}-$$.log"
  push_built_image "${remote_image}" "${push_log}"
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
  record_built_image "${image_key}_DIGEST" "${digest}"
}
