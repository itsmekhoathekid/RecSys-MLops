#!/usr/bin/env bash
set -euo pipefail

# Rebuild the exact public upstream revision referenced by llama.cpp#26530.
# No RecSys source is copied into this image or sent as Docker build context.
revision="0c58ba3365d2bc717b447b5d70e4d6be09ff3c40"
project_id="${PROJECT_ID:-recsys-mlops-506406}"
image="asia-southeast1-docker.pkg.dev/${project_id}/recsys/llama-cpp:server-b8646-0c58ba3-cpu"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/llama-cpp-b8646.XXXXXX")"

cleanup() {
  if [[ -n "${build_dir}" && "${build_dir}" == *llama-cpp-b8646.* ]]; then
    rm -rf -- "${build_dir}"
  fi
}
trap cleanup EXIT

git clone --filter=blob:none https://github.com/ggml-org/llama.cpp.git "${build_dir}"
git -C "${build_dir}" checkout --detach "${revision}"
test "$(git -C "${build_dir}" rev-parse HEAD)" = "${revision}"
test -z "$(git -C "${build_dir}" status --porcelain)"

docker buildx build \
  --platform linux/amd64 \
  --target server \
  --file "${build_dir}/.devops/cpu.Dockerfile" \
  --tag "${image}" \
  --push \
  "${build_dir}"

crane digest "${image}"
