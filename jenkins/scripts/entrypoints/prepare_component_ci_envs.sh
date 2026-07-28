#!/usr/bin/env bash
set -euo pipefail

source jenkins/scripts/lib/common.sh

ci_tmp_root="${CI_TMP_ROOT:?CI_TMP_ROOT is required}"
changed_components="${CHANGED_COMPONENTS:-}"
[[ -n "${changed_components}" ]] || {
  recsys_error "CHANGED_COMPONENTS is required"
  exit 2
}

profiles=()
while IFS= read -r profile; do
  [[ -n "${profile}" ]] && profiles+=("${profile}")
done < <(
  python3 jenkins/python/configuration.py ci-profiles \
    --components "${changed_components}"
)

for profile in "${profiles[@]}"; do
  [[ -n "${profile}" ]] || continue
  profile_json="$(python3 jenkins/python/configuration.py ci-profile "${profile}")"
  project_path="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["projectPath"])' \
      <<<"${profile_json}"
  )"
  lock_file="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["lockFile"])' \
      <<<"${profile_json}"
  )"
  python_version="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["pythonVersion"])' \
      <<<"${profile_json}"
  )"
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
done
