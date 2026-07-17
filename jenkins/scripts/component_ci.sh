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

  COVERAGE_FILE="${reports_dir}/coverage/.coverage.${name}" \
  PYTHONPATH="${pythonpath}" uv run --no-sync pytest "${test_paths[@]}" -q \
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
    uv run --no-sync python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py \
      --package-path "${package_path}"

  PYTHONPATH=apps/ml-system/src:apps/data-platform/src \
    uv run --no-sync python apps/ml-system/src/kubeflow/validate_pipeline_package.py \
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
  PYTHONPATH="${pythonpath}" uv run --no-sync pytest "$@" -q \
    --junitxml="${reports_dir}/junit/${name}.xml"
}

run_plain_pytest_with_pythonpath_override() {
  local name="$1"
  local pythonpath="$2"
  shift 2
  PYTHONPATH="${pythonpath}" uv run --no-sync pytest "$@" -q \
    -o "pythonpath=${pythonpath}" \
    --junitxml="${reports_dir}/junit/${name}.xml"
}

case "${component}" in
  materialize)
    tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir materialize
    cov_paths=(feature_store.online_writer local.run_batch_features)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  training)
    tests=(tests/unit/ml_system)
    append_integration_dir training
    cov_paths=(kubeflow.components.runtime kubeflow.pipelines.bst_training_pipeline kubeflow.pipelines.compile_training_pipeline)
    component_pytest "${component}" "apps/ml-system/src:apps/data-platform/src"
    run_kfp_compile
    ;;
  spark_batch)
    tests=(tests/unit/data_platform/test_data_platform.py tests/unit/data_platform/test_spark_schema_merge.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir spark_batch
    cov_paths=(lakehouse.iceberg)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  dp1)
    run_plain_pytest_with_pythonpath_override "dp1-data-generator" "apps/data-platform/data-generator/src" tests/unit/data_generator
    tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir dp1
    cov_paths=(ingest.debezium ingest.batch_lakehouse_ingestion)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  dp2)
    tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir dp2
    cov_paths=(lakehouse.iceberg)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  dp3)
    tests=(tests/unit/data_platform/test_data_platform.py tests/unit/ml_system/test_prepare_bst_training_data.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir dp3
    cov_paths=(lakehouse.iceberg feature_store.online_writer)
    component_pytest "${component}" "apps/ml-system/src:apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  api)
    tests=(tests/unit/api_serving tests/contract/test_serving_contracts.py tests/contract/test_gateway_contracts.py)
    append_integration_dir api
    cov_paths=(ab_testing api_runtime api_schemas feature_api feature_service_client inference_api online_features ranking serving_utils shadow triton)
    component_pytest "${component}" "apps/api-serving/src"
    ;;
  kserve)
    tests=(tests/unit/ml_system/test_model_promotion.py tests/contract/test_serving_contracts.py)
    append_integration_dir kserve
    cov_paths=(model_cd)
    component_pytest "${component}" "jenkins/scripts:apps/ml-system/src:apps/data-platform/src"
    ;;
  rollout)
    tests=(tests/unit/ml_system/test_model_rollout_controller.py tests/contract/test_serving_contracts.py)
    append_integration_dir rollout
    # Controller behavior is exercised by the rollout unit suite; model_cd is
    # the deploy executor with the enforced per-component 90% coverage gate.
    cov_paths=(model_cd)
    component_pytest "${component}" "jenkins/scripts:apps/ml-system/src:apps/data-platform/src"
    helm lint infra/helm/recsys-ci
    helm template recsys-ci infra/helm/recsys-ci \
      --set modelRolloutWatcher.enabled=true \
      --set modelRolloutWatcher.image=registry.example/recsys-mlops-training:ci >/dev/null
    helm lint infra/helm/recsys-serving
    bash -n jenkins/scripts/autonomous_rollout_locust.sh
    bash -n jenkins/scripts/model_rollout_demo.sh
    bash -n jenkins/scripts/verify_champion_only.sh
    ;;
  drift)
    tests=(tests/unit/data_generator/test_drift_reporting_unit.py)
    append_integration_dir drift
    cov_paths=(drift.controller drift.reporting)
    component_pytest "${component}" "apps/data-platform/data-generator/src:apps/data-platform/src"
    run_plain_pytest "drift-data-platform" "apps/data-platform/src:apps/data-platform/data-generator/src" tests/unit/data_platform/test_data_platform.py
    ;;
  stream_offline)
    tests=(tests/unit/data_platform/test_data_platform.py tests/unit/data_platform/test_flink_event_time.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir stream_offline
    cov_paths=(features.flink.candidate_pool_job features.flink.item_features_job features.flink.user_aggregate_job features.flink.user_sequence_job features.flink.time_utils lakehouse.iceberg)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
    ;;
  stream_online)
    tests=(tests/unit/data_platform/test_data_platform.py tests/unit/data_platform/test_flink_event_time.py tests/unit/api_serving/test_serving.py tests/contract/test_docker_dataflow_contracts.py)
    append_integration_dir stream_online
    cov_paths=(features.flink.candidate_pool_job features.flink.item_features_job features.flink.user_aggregate_job features.flink.user_sequence_job features.flink.time_utils feature_store.online_writer)
    component_pytest "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src:apps/api-serving/src"
    ;;
  analytics)
    run_plain_pytest "analytics" "apps/analytics/src:apps/data-platform/src" \
      tests/unit/analytics tests/contract/test_analytics_contracts.py
    helm lint infra/helm/recsys-analytics
    helm template recsys-analytics infra/helm/recsys-analytics >/dev/null
    ;;
  demo_web)
    demo_backend_env="${PWD}/apps/demo-web/backend/.venv"
    PYTHONPATH=apps/demo-web/backend UV_PROJECT_ENVIRONMENT="${demo_backend_env}" \
      uv run --project apps/demo-web/backend ruff check apps/demo-web/backend/app apps/demo-web/backend/tests
    PYTHONPATH=apps/demo-web/backend UV_PROJECT_ENVIRONMENT="${demo_backend_env}" \
      uv run --project apps/demo-web/backend ruff format --check apps/demo-web/backend/app apps/demo-web/backend/tests
    UV_PROJECT_ENVIRONMENT="${demo_backend_env}" uv run --project apps/demo-web/backend pip-audit
    PYTHONPATH=apps/demo-web/backend UV_PROJECT_ENVIRONMENT="${demo_backend_env}" \
      uv run --project apps/demo-web/backend pytest \
      apps/demo-web/backend/tests tests/contract/test_demo_web_contracts.py -q \
      --cov=apps/demo-web/backend/app \
      --cov-report="xml:${reports_dir}/coverage/demo_web_backend.xml" \
      --cov-fail-under="${coverage_min}" \
      --junitxml="${reports_dir}/junit/demo_web_backend.xml"
    # Production is always built from the Node 24 Dockerfile. Node 22 remains
    # supported for fast local/static gates when Docker Hub is unavailable.
    if command -v node >/dev/null 2>&1 && [[ "$(node -p 'process.versions.node.split(`.`)[0]')" -ge 22 ]]; then
      run_demo_frontend() {
        (cd apps/demo-web/frontend && "$@")
      }
      copy_demo_frontend_coverage() {
        cp -R apps/demo-web/frontend/coverage/. "${reports_dir}/coverage/demo_web_frontend/"
      }
    else
      frontend_container="recsys-demo-web-ci-${BUILD_NUMBER:-$$}"
      frontend_container="${frontend_container//[^a-zA-Z0-9_.-]/-}"
      docker rm -f "${frontend_container}" >/dev/null 2>&1 || true
      docker create --name "${frontend_container}" -w /workspace \
        node:24-bookworm-slim sleep infinity >/dev/null
      docker start "${frontend_container}" >/dev/null
      docker exec "${frontend_container}" mkdir -p \
        /workspace/apps/demo-web/frontend /workspace/apps/demo-web/backend
      docker cp apps/demo-web/frontend/. \
        "${frontend_container}:/workspace/apps/demo-web/frontend"
      docker cp apps/demo-web/backend/openapi.json \
        "${frontend_container}:/workspace/apps/demo-web/backend/openapi.json"
      cleanup_demo_frontend() {
        docker rm -f "${frontend_container}" >/dev/null 2>&1 || true
      }
      trap cleanup_demo_frontend EXIT
      run_demo_frontend() {
        docker exec -e HOME=/tmp -w /workspace/apps/demo-web/frontend \
          "${frontend_container}" "$@"
      }
      copy_demo_frontend_coverage() {
        docker cp "${frontend_container}:/workspace/apps/demo-web/frontend/coverage/." \
          "${reports_dir}/coverage/demo_web_frontend/"
      }
    fi
    run_demo_frontend npm ci
    run_demo_frontend npm audit --audit-level=high
    run_demo_frontend npm run lint
    run_demo_frontend npm run format:check
    run_demo_frontend npm run typecheck
    run_demo_frontend npm test
    run_demo_frontend npm run build
    mkdir -p "${reports_dir}/coverage/demo_web_frontend"
    copy_demo_frontend_coverage
    helm lint infra/helm/recsys-demo-web -f infra/helm/recsys-demo-web/values-gcp.yaml
    helm template recsys-demo-web infra/helm/recsys-demo-web \
      -f infra/helm/recsys-demo-web/values-gcp.yaml --namespace api-serving >/dev/null
    ;;
  *)
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac
