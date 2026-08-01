#!/usr/bin/env bash

ci_materialize() {
  tests=(
    tests/unit/data_platform/test_data_platform.py
    tests/unit/feature_store/test_bigquery_feature_repo.py
    tests/unit/feature_store/test_sql_registry_state.py
    tests/contract/test_docker_dataflow_contracts.py
  )
  append_integration_dir materialize
  cov_paths=(feature_store.online_writer feature_store.sql_registry_state)
  run_configured_component_tests "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"

  local data_platform_src="${PWD}/apps/data-platform/src"
  local feast_repo="${PWD}/apps/data-platform/feature-store/feature_repo"
  local registry_path="${CI_TMP_ROOT}/feast-registry-${component}.db"
  local feast_log="${reports_dir}/feast-${component}.log"
  rm -f "${registry_path}"
  (
    export PYTHONPATH="${data_platform_src}:${PYTHONPATH:-}"
    export FEAST_SQL_REGISTRY_URL="sqlite:///${registry_path}"
    MPLCONFIGDIR="${CI_TMP_ROOT}/matplotlib" \
      "${ci_environment}/bin/feast" -c "${feast_repo}" \
        plan --skip-source-validation
    MPLCONFIGDIR="${CI_TMP_ROOT}/matplotlib" \
      "${ci_environment}/bin/feast" -c "${feast_repo}" \
        apply --skip-source-validation --no-progress
    "${ci_python}" -m feature_store.sql_registry_state verify --project recsys
  ) 2>&1 | tee "${feast_log}"
}

ci_dp1() {
  run_plain_pytest_with_pythonpath_override "dp1-data-generator" "apps/data-platform/data-generator/src" tests/unit/data_generator
  tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
  append_integration_dir dp1
  cov_paths=(ingest.debezium ingest.batch_lakehouse_ingestion)
  run_configured_component_tests "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
}

ci_dp2() {
  tests=(tests/unit/data_platform/test_data_platform.py tests/contract/test_docker_dataflow_contracts.py)
  append_integration_dir dp2
  cov_paths=(lakehouse.iceberg)
  run_configured_component_tests "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
}

ci_dp3() {
  tests=(tests/unit/data_platform/test_data_platform.py tests/unit/ml_system/test_prepare_bst_training_data.py tests/contract/test_docker_dataflow_contracts.py)
  append_integration_dir dp3
  cov_paths=(lakehouse.iceberg feature_store.online_writer)
  run_configured_component_tests "${component}" "apps/ml-system/src:apps/data-platform/src:apps/data-platform/data-generator/src"
}

ci_drift() {
  tests=(tests/unit/data_generator/test_drift_reporting_unit.py)
  append_integration_dir drift
  cov_paths=(drift)
  run_configured_component_tests "${component}" "apps/data-platform/data-generator/src:apps/data-platform/src"
  run_plain_pytest "drift-data-platform" "apps/data-platform/src:apps/data-platform/data-generator/src" tests/unit/data_platform/test_data_platform.py
}

ci_stream_offline() {
  tests=(tests/unit/data_platform/test_data_platform.py tests/unit/data_platform/test_flink_event_time.py tests/contract/test_docker_dataflow_contracts.py)
  append_integration_dir stream_offline
  cov_paths=(features.flink.features.candidate_pool features.flink.features.item features.flink.features.user_aggregate features.flink.features.user_sequence features.flink.time_utils lakehouse.iceberg)
  run_configured_component_tests "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src"
}

ci_stream_online() {
  tests=(tests/unit/data_platform/test_data_platform.py tests/unit/data_platform/test_flink_event_time.py tests/unit/api_serving/test_serving.py tests/contract/test_docker_dataflow_contracts.py)
  append_integration_dir stream_online
  cov_paths=(features.flink.features.candidate_pool features.flink.features.item features.flink.features.user_aggregate features.flink.features.user_sequence features.flink.time_utils feature_store.online_writer)
  run_configured_component_tests "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src:apps/api-serving/src"
}
