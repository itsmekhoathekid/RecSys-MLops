#!/usr/bin/env bash

record_built_image() {
  image_manifest_record "${BUILD_MANIFEST_PATH}" "$1" "$2"
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

validate_built_image() {
  local name="$1"
  local local_image="$2"

  case "${name}" in
    recsys-online-feature-api|recsys-inference-api|recsys-rag-api|recsys-feature-rag-mcp|recsys-recommendation-mcp)
      bash jenkins/scripts/test/serving_images.sh "${local_image}" "${name}"
      ;;
    recsys-rag-admin)
      bash jenkins/scripts/test/rag_admin_image.sh "${local_image}"
      ;;
  esac
}

reuse_published_image() {
  local remote_image="$1"
  local local_image="$2"
  local digest_reference="$3"

  if ! docker pull --platform "${BUILD_DOCKER_PLATFORM}" "${digest_reference}"; then
    recsys_log \
      "Docker pull failed for ${digest_reference}; refreshing registry login and retrying once"
    registry_login_gcp "${BUILD_IMAGE_REGISTRY}" >/dev/null
    BUILD_REGISTRY_LOGIN_EPOCH="$(date +%s)"
    export BUILD_REGISTRY_LOGIN_EPOCH
    docker pull --platform "${BUILD_DOCKER_PLATFORM}" "${digest_reference}"
  fi
  docker tag "${digest_reference}" "${local_image}"
  recsys_log BUILD \
    "reusing immutable image for full-SHA tag ${remote_image}: ${digest_reference}"
}

build_publish_image() {
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

  image_key="$(image_manifest_key "${name}")"
  if recsys_is_true "${BUILD_PUBLISH_IMAGES}" \
    && [[ "${BUILD_REGISTRY_HOST}" == *".pkg.dev" ]] \
    && digest="$(registry_resolve_digest_reference "${remote_image}" "${BUILD_IMAGE_REGISTRY}" 2>/dev/null)"; then
    reuse_published_image "${remote_image}" "${local_image}" "${digest}"
    validate_built_image "${name}" "${local_image}"
    record_built_image "${image_key}_LOCAL_IMAGE" "${local_image}"
    record_built_image "${image_key}_IMAGE" "${remote_image}"
    record_built_image "${image_key}_DIGEST" "${digest}"
    return 0
  fi

  docker build "${docker_args[@]}" -f "${dockerfile}" -t "${local_image}" "${context}"
  validate_built_image "${name}" "${local_image}"
  docker tag "${local_image}" "${remote_image}"
  record_built_image "${image_key}_LOCAL_IMAGE" "${local_image}"
  record_built_image "${image_key}_IMAGE" "${remote_image}"
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
