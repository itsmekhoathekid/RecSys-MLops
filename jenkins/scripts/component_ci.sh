#!/usr/bin/env bash
set -euo pipefail

component="${1:?component is required}"
coverage_min="${COVERAGE_MIN:-90}"
reports_dir="${REPORTS_DIR:-reports}"
mkdir -p "${reports_dir}/junit" "${reports_dir}/coverage"
ci_profile="$(
  python3 -c 'import json,sys; print(json.load(sys.stdin)["ciProfile"])' \
    <<<"$(python3 jenkins/python/configuration.py component "${component}")"
)"
ci_environment="${CI_TMP_ROOT:?CI_TMP_ROOT is required}/envs/${ci_profile}"
ci_python="${ci_environment}/bin/python"
[[ -x "${ci_python}" ]] || {
  echo "Locked CI environment is missing for ${component}: ${ci_environment}" >&2
  exit 2
}
export UV_PROJECT_ENVIRONMENT="${ci_environment}"
export PYSPARK_PYTHON="${ci_python}"
export PYSPARK_DRIVER_PYTHON="${ci_python}"

has_tests() {
  local path="$1"
  [[ -d "${path}" ]] && find "${path}" -name 'test_*.py' -type f | grep -q .
}

append_integration_dir() {
  local name="$1"
  local path="tests/integration/${name}"
  if has_tests "${path}"; then
    tests+=("${path}")
  else
    echo "No integration tests found at ${path}; using component unit/contract gates only."
  fi
}

run_component_pytest() {
  local name="$1"
  local pythonpath="$2"
  shift 2
  local cov_paths=()
  local test_paths=()

  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --cov-path)
        cov_paths+=("$2")
        shift 2
        ;;
      --test-path)
        test_paths+=("$2")
        shift 2
        ;;
      *)
        echo "Unknown run_component_pytest argument: $1" >&2
        return 2
        ;;
    esac
  done

  if [[ "${#test_paths[@]}" -eq 0 ]]; then
    echo "No tests configured for ${name}" >&2
    return 2
  fi

  local cov_args=()
  for cov_path in "${cov_paths[@]}"; do
    cov_args+=(--cov "${cov_path}")
  done

  COVERAGE_FILE="${reports_dir}/coverage/.coverage.${name}" \
  PYTHONPATH="${pythonpath}" "${ci_python}" -m pytest "${test_paths[@]}" -q \
    -o "pythonpath=${pythonpath}" \
    --cov-config="${PWD}/pyproject.toml" \
    "${cov_args[@]}" \
    --cov-report="term-missing" \
    --cov-report="xml:${reports_dir}/coverage/${name}.xml" \
    --cov-fail-under="${coverage_min}" \
    --junitxml="${reports_dir}/junit/${name}.xml"
}

component_pytest() {
  local name="$1"
  local pythonpath="$2"
  local args=()

  for cov_path in "${cov_paths[@]}"; do
    args+=(--cov-path "${cov_path}")
  done
  for test_path in "${tests[@]}"; do
    args+=(--test-path "${test_path}")
  done

  run_component_pytest "${name}" "${pythonpath}" "${args[@]}"
}

run_kfp_compile() {
  local training_image="${RECSYS_PIPELINE_IMAGE:-ci-registry.example/recsys/recsys-mlops-training:ci}"
  local ray_image="${RECSYS_RAY_IMAGE:-${training_image}}"
  local spark_image="${RECSYS_SPARK_IMAGE:-ci-registry.example/recsys/recsys-mlops-spark:ci}"
  local package_path="${KFP_CI_PACKAGE_PATH:-${reports_dir}/bst_training_pipeline.${component}.yaml}"

  PYTHONPATH=apps/ml-system/src:apps/data-platform/src \
    RECSYS_PIPELINE_IMAGE="${training_image}" \
    RECSYS_RAY_IMAGE="${ray_image}" \
    RECSYS_SPARK_IMAGE="${spark_image}" \
    "${ci_python}" apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py \
      --package-path "${package_path}"

  PYTHONPATH=apps/ml-system/src:apps/data-platform/src \
    "${ci_python}" apps/ml-system/src/kubeflow/validate_pipeline_package.py \
      --package-path "${package_path}" \
      --required-image "${training_image}" \
      --required-image "${ray_image}" \
      --required-image "${spark_image}" \
      --forbidden-token ":local"
}

run_plain_pytest() {
  local name="$1"
  local pythonpath="$2"
  shift 2
  PYTHONPATH="${pythonpath}" "${ci_python}" -m pytest "$@" -q \
    --junitxml="${reports_dir}/junit/${name}.xml"
}

run_plain_pytest_with_pythonpath_override() {
  local name="$1"
  local pythonpath="$2"
  shift 2
  PYTHONPATH="${pythonpath}" "${ci_python}" -m pytest "$@" -q \
    -o "pythonpath=${pythonpath}" \
    --junitxml="${reports_dir}/junit/${name}.xml"
}

source jenkins/scripts/ci/data.sh
source jenkins/scripts/ci/ml.sh
source jenkins/scripts/ci/serving.sh
source jenkins/scripts/ci/demo.sh
source jenkins/scripts/ci/analytics.sh
source jenkins/scripts/ci/dispatch.sh

ci_dispatch "${component}"
migration_args=(--component "${component}")
if [[ -n "${CI_BASE_REF:-}" ]]; then
  migration_args+=(--base-ref "${CI_BASE_REF}")
fi
python3 -m jenkins.python.migration_policy "${migration_args[@]}"
