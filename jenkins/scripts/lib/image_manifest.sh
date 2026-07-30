#!/usr/bin/env bash

image_manifest_key() {
  printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_'
}

image_manifest_lookup() {
  local image_name="$1"
  local manifest_dir="${IMAGE_MANIFEST_DIR:-.ci-image-manifest}"
  local key
  local value=""
  key="$(image_manifest_key "${image_name}")"

  if [[ -d "${manifest_dir}" ]]; then
    value="$(
      awk -F= -v digest_key="${key}_DIGEST" '
        $1 == digest_key {
          sub(/^[^=]*=/, "", $0)
          print
          exit
        }
      ' "${manifest_dir}"/*.env 2>/dev/null || true
    )"
    if [[ -z "${value}" ]]; then
      value="$(
        awk -F= -v image_key="${key}_IMAGE" '
          $1 == image_key {
            sub(/^[^=]*=/, "", $0)
            print
            exit
          }
        ' "${manifest_dir}"/*.env 2>/dev/null || true
      )"
    fi
  fi
  printf '%s' "${value}"
}

image_manifest_record() {
  local manifest_path="$1"
  local key="$2"
  local value="$3"
  local temporary_path="${manifest_path}.tmp.$$"

  awk -F= -v key="${key}" '$1 != key' "${manifest_path}" >"${temporary_path}"
  printf '%s=%s\n' "${key}" "${value}" >>"${temporary_path}"
  mv -f "${temporary_path}" "${manifest_path}"
}
