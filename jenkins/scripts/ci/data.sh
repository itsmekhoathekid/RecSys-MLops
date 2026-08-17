#!/usr/bin/env bash

ci_materialize() {
  tests=(
    tests/unit/data_platform/test_data_platform.py
    tests/unit/feature_store/test_bigquery_feature_repo.py
    tests/unit/feature_store/test_sql_registry_state.py
    tests/contract/test_docker_dataflow_contracts.py
  )
  append_integration_dir materialize
  cov_paths=(feature_store.online_writer recsys_feature_store_runtime.sql_registry_state)
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
    "${ci_python}" -m recsys_feature_store_runtime.sql_registry_state verify --project recsys
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
  tests=(tests/unit/data_platform/test_data_platform.py tests/unit/data_platform/test_flink_event_time.py tests/contract/test_stream_online_serving_contract.py tests/contract/test_docker_dataflow_contracts.py)
  append_integration_dir stream_online
  cov_paths=(features.flink.features.candidate_pool features.flink.features.item features.flink.features.user_aggregate features.flink.features.user_sequence features.flink.time_utils feature_store.online_writer)
  run_configured_component_tests "${component}" "apps/data-platform/src:apps/data-platform/data-generator/src:apps/api-serving/shared/src:apps/api-serving/online-feature-api/src:packages/recsys-feature-store-runtime/src"
}

ci_rag_index() {
  tests=(
    tests/unit/data_platform/rag_data
    tests/unit/data_platform/test_governance_lineage.py
    tests/unit/jenkins/test_rag_retrieval_verifier.py
    tests/unit/test_runtime_lineage.py
  )
  cov_paths=(
    rag_data.pipeline_contracts
    rag_data.semantic_chunker
    rag_data.index_lifecycle
  )
  run_configured_component_tests "${component}" "apps/data-platform/src:apps/data-platform/feature-store/rag_feature_repo:packages/recsys-rag-runtime/src"

  PYTHONPATH="apps/data-platform/src" "${ci_python}" \
    -m metadata.ingest_datahub_governance --verify-only
  "${ci_python}" -m py_compile \
    apps/data-platform/src/orchestration/airflow/dags/recsys_rag_item_index.py \
    apps/data-platform/src/orchestration/airflow/dags/recsys_rag_item_reconciliation.py

  "${ci_environment}/bin/interrogate" \
    --fail-under 90 \
    --ignore-init-method \
    --ignore-private \
    --ignore-semiprivate \
    --ignore-property-decorators \
    apps/data-platform/src/rag_data \
    packages/recsys-rag-runtime/src

  local rag_registry="${CI_TMP_ROOT}/rag-feast-registry.db"
  FEAST_SQL_REGISTRY_URL="sqlite:///${rag_registry}" \
  MILVUS_HOST="http://127.0.0.1" \
  MILVUS_USERNAME="" \
  MILVUS_PASSWORD="" \
    "${ci_environment}/bin/feast" \
      -c apps/data-platform/feature-store/rag_feature_repo \
      plan --skip-source-validation

  PYTHONPATH="apps/data-platform/feature-store/rag_feature_repo:${PYTHONPATH:-}" \
    "${ci_environment}/bin/feast" \
      -c tests/fixtures/rag_feature_repo \
      apply --skip-source-validation --no-progress

  local rag_compose="tests/integration/rag_index/docker-compose.yaml"
  local rag_project="recsys-rag-${BUILD_NUMBER:-local}"
  local integration_status=0
  (
    local rag_minio_address rag_milvus_address rag_ready=0
    # Keep the ephemeral services recoverable on every exit path, including
    # readiness and port-discovery failures before pytest starts.
    trap 'docker compose -p "${rag_project}" -f "${rag_compose}" down --volumes --remove-orphans' EXIT
    docker compose -p "${rag_project}" -f "${rag_compose}" up -d
    rag_minio_address="$(docker compose -p "${rag_project}" -f "${rag_compose}" port minio 9000)"
    rag_milvus_address="$(docker compose -p "${rag_project}" -f "${rag_compose}" port milvus 19530)"
    rag_minio_address="${rag_minio_address/0.0.0.0/127.0.0.1}"
    rag_milvus_address="${rag_milvus_address/0.0.0.0/127.0.0.1}"
    for _ in {1..60}; do
      if curl --fail --silent "http://${rag_minio_address}/minio/health/live" >/dev/null \
        && "${ci_python}" -c "from pymilvus import MilvusClient; client=MilvusClient('http://${rag_milvus_address}'); client.list_collections(); client.close()"; then
        rag_ready=1
        break
      fi
      sleep 2
    done
    if [[ "${rag_ready}" != "1" ]]; then
      docker compose -p "${rag_project}" -f "${rag_compose}" logs
      exit 1
    fi
    export RAG_TEST_MINIO_ENDPOINT="http://${rag_minio_address}"
    export RAG_TEST_MILVUS_URI="http://${rag_milvus_address}"
    run_plain_pytest \
      "rag-index-integration" \
      "apps/data-platform/src:apps/data-platform/feature-store/rag_feature_repo:packages/recsys-rag-runtime/src" \
      tests/integration/rag_index
  ) || integration_status=$?
  [[ "${integration_status}" == "0" ]] || return "${integration_status}"

  # A clean Jenkins agent has no user-scoped Helm repositories. Register the
  # locked upstream before rebuilding the vendored Milvus dependency.
  helm repo add milvus https://zilliztech.github.io/milvus-helm/ --force-update
  helm dependency build --skip-refresh infra/helm/recsys-milvus
  helm lint infra/helm/recsys-milvus -f infra/helm/recsys-milvus/values-gcp.yaml
  helm template recsys-milvus infra/helm/recsys-milvus \
    -f infra/helm/recsys-milvus/values-gcp.yaml >/dev/null
}
