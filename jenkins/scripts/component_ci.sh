#!/usr/bin/env bash
set -euo pipefail

component="${1:?component is required}"
coverage_min="${COVERAGE_MIN:-90}"
reports_dir="${REPORTS_DIR:-reports}"
mkdir -p "${reports_dir}/junit" "${reports_dir}/coverage"

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

  PYTHONPATH="${pythonpath}" uv run pytest "${test_paths[@]}" -q \
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
  PYTHONPATH=apps/ml-system/src:apps/data-platform/src \
    uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py
}

case "${component}" in
  materialize)
    tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir materialize
    cov_paths=(apps/data-platform/src/feature_store apps/data-platform/src/local)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  training)
    tests=(tests/unit/ml_system)
    append_integration_dir training
    cov_paths=(apps/ml-system/src)
    component_pytest "${component}" "apps/ml-system/src:apps/data-platform/src"
    run_kfp_compile
    ;;
  spark_batch)
    tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir spark_batch
    cov_paths=(apps/data-platform/src/feature_engineering/spark apps/data-platform/src/lakehouse)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  dp1)
    tests=(tests/unit/data_generator tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir dp1
    cov_paths=(apps/data-platform/data-generator/src apps/data-platform/src/ingest)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  dp2)
    tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir dp2
    cov_paths=(apps/data-platform/src/feature_engineering/spark apps/data-platform/src/lakehouse)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  dp3)
    tests=(tests/unit/data_platform/test_data_platform.py tests/unit/ml_system/test_prepare_bst_training_data.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir dp3
    cov_paths=(apps/data-platform/src/feature_engineering/spark apps/data-platform/src/feature_store apps/ml-system/src)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src:apps/ml-system/src"
    ;;
  api)
    tests=(tests/unit/api_serving tests/contract/test_serving_contracts.py tests/contract/test_gateway_contracts.py)
    append_integration_dir api
    cov_paths=(apps/api-serving/src)
    component_pytest "${component}" "apps/api-serving/src"
    ;;
  kserve)
    tests=(tests/unit/ml_system/test_model_promotion.py tests/contract/test_serving_contracts.py)
    append_integration_dir kserve
    cov_paths=(apps/ml-system/src jenkins/scripts)
    component_pytest "${component}" "apps/ml-system/src:apps/data-platform/src"
    ;;
  drift)
    tests=(tests/unit/data_generator/test_drift.py tests/unit/data_platform/test_data_platform.py)
    append_integration_dir drift
    cov_paths=(apps/data-platform/src/validate apps/data-platform/src/mlops apps/data-platform/data-generator/src/drift)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  stream_offline)
    tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir stream_offline
    cov_paths=(apps/data-platform/src/feature_engineering/flink apps/data-platform/src/lakehouse)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  stream_online)
    tests=(tests/unit/data_platform/test_data_platform.py tests/unit/api_serving/test_serving.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir stream_online
    cov_paths=(apps/data-platform/src/feature_engineering/flink apps/data-platform/src/feature_store)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src:apps/api-serving/src"
    ;;
  *)
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac
