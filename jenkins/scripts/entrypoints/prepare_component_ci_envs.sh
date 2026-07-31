#!/usr/bin/env bash
set -euo pipefail

source jenkins/scripts/lib/common.sh

ci_tmp_root="${CI_TMP_ROOT:?CI_TMP_ROOT is required}"
changed_components="${CHANGED_COMPONENTS:-}"
[[ -n "${changed_components}" ]] || {
  recsys_error "CHANGED_COMPONENTS is required"
  exit 2
}

while IFS=$'\t' read -r profile project_path lock_file python_version; do
  [[ -n "${profile}" ]] || continue
  environment_path="${ci_tmp_root}/envs/${profile}"

  [[ -f "${project_path}/pyproject.toml" && -f "${lock_file}" ]] || {
    recsys_error "CI profile ${profile} is missing pyproject.toml or uv.lock"
    exit 2
  }
  recsys_log "syncing locked ${profile} CI environment from ${lock_file}"
  UV_PROJECT_ENVIRONMENT="${environment_path}" \
    uv sync \
      --project "${project_path}" \
      --frozen \
      --group dev \
      --no-install-project \
      --python "${python_version}"
done < <(
  python3 jenkins/python/configuration.py ci-profiles \
    --components "${changed_components}"
)
