#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source_commit="${KAGENT_SOURCE_COMMIT:-e6df917e9fa8}"
project_id="${PROJECT_ID:-recsys-mlops-506406}"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/recsys-kagent-build.XXXXXX")"

cleanup() {
  if [[ -n "${build_dir}" && "${build_dir}" == *recsys-kagent-build.* ]]; then
    rm -rf -- "${build_dir}"
  fi
}
trap cleanup EXIT

git clone --filter=blob:none https://github.com/kagent-dev/kagent.git "${build_dir}"
git -C "${build_dir}" checkout --detach "${source_commit}"
git -C "${build_dir}" apply --check "${repo_root}/ops/gcp/patches/kagent-e6df917-substrate0011.patch"
git -C "${build_dir}" apply "${repo_root}/ops/gcp/patches/kagent-e6df917-substrate0011.patch"
cp "${repo_root}/ops/gcp/cloudbuild_kagent_source.yaml" "${build_dir}/cloudbuild.recsys.yaml"
cp "${repo_root}/ops/gcp/kagent_cloudbuild.ignore" "${build_dir}/.gcloudignore"

if [[ "${KAGENT_SKIP_LOCAL_TESTS:-false}" != "true" ]]; then
  (
    cd "${build_dir}/go"
    go test ./adk/pkg/agent ./core/pkg/sandboxbackend/substrate
  )
fi

gcloud builds submit "${build_dir}" \
  --project="${project_id}" \
  --config="${build_dir}/cloudbuild.recsys.yaml" \
  --ignore-file="${build_dir}/.gcloudignore"
