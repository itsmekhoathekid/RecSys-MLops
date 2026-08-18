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

ci_datahub_catalog() {
  tests=(
    tests/unit/data_platform/test_datahub_catalog.py
    tests/unit/data_platform/test_datahub_validation_publisher.py
    tests/unit/data_platform/test_datahub_dataset_cutover.py
    tests/unit/data_platform/test_governance_contracts.py
    tests/contract/test_docker_dataflow_contracts.py
  )
  cov_paths=(metadata.governance_catalog metadata.datahub_client metadata.sync_datahub_catalog metadata.publish_datahub_validation validate.report_io)
  run_configured_component_tests "${component}" "apps/data-platform/src"
  PYTHONPATH="apps/data-platform/src" "${ci_python}" -c \
    'from metadata.governance_catalog import catalog_products, validate_catalog; print(validate_catalog(catalog_products()))'
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
    tests/unit/jenkins/test_rag_retrieval_verifier.py
  )
  cov_paths=(
    rag_data.pipeline_contracts
    rag_data.semantic_chunker
    rag_data.index_lifecycle
  )
  run_configured_component_tests "${component}" "apps/data-platform/src:apps/data-platform/feature-store/rag_feature_repo:packages/recsys-rag-runtime/src"

  PYTHONPATH="apps/data-platform/src" "${ci_python}" -c \
    'from metadata.governance_catalog import catalog_products, validate_catalog; print(validate_catalog(catalog_products()))'
  "${ci_python}" -m py_compile \
    apps/data-platform/src/orchestration/airflow/dags/recsys_rag_item_index.py

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
    local rag_runtime="compose"
    local rag_network="${rag_project}-network"
    local rag_etcd_container="${rag_project}-etcd"
    local rag_minio_container="${rag_project}-minio"
    local rag_milvus_container="${rag_project}-milvus"

    rag_cleanup() {
      if [[ "${rag_runtime}" == "compose" ]]; then
        docker compose -p "${rag_project}" -f "${rag_compose}" \
          down --volumes --remove-orphans >/dev/null 2>&1 || true
        return
      fi
      docker rm -f \
        "${rag_milvus_container}" \
        "${rag_minio_container}" \
        "${rag_etcd_container}" >/dev/null 2>&1 || true
      docker network rm "${rag_network}" >/dev/null 2>&1 || true
    }

    rag_logs() {
      if [[ "${rag_runtime}" == "compose" ]]; then
        docker compose -p "${rag_project}" -f "${rag_compose}" logs || true
        return
      fi
      docker logs "${rag_etcd_container}" || true
      docker logs "${rag_minio_container}" || true
      docker logs "${rag_milvus_container}" || true
    }

    rag_start_services() {
      if docker compose version >/dev/null 2>&1; then
        docker compose -p "${rag_project}" -f "${rag_compose}" up -d || return 1
        rag_minio_address="$(docker compose -p "${rag_project}" -f "${rag_compose}" port minio 9000)" || return 1
        rag_milvus_address="$(docker compose -p "${rag_project}" -f "${rag_compose}" port milvus 19530)" || return 1
        return
      fi

      # The production Jenkins image intentionally ships only the Docker CLI.
      # Recreate the compose topology with isolated, build-scoped resources so
      # the RAG integration gate remains a real Milvus/MinIO test.
      rag_runtime="docker"
      docker network create "${rag_network}" >/dev/null || return 1
      docker run -d \
        --name "${rag_etcd_container}" \
        --network "${rag_network}" \
        --network-alias etcd \
        quay.io/coreos/etcd:v3.5.18 \
        etcd --advertise-client-urls=http://etcd:2379 \
        --listen-client-urls=http://0.0.0.0:2379 \
        --data-dir=/etcd >/dev/null || return 1
      docker run -d \
        --name "${rag_minio_container}" \
        --network "${rag_network}" \
        --network-alias minio \
        -p 127.0.0.1::9000 \
        -e MINIO_ROOT_USER=minio \
        -e MINIO_ROOT_PASSWORD=minio123 \
        quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z \
        minio server /data --console-address :9001 >/dev/null || return 1
      docker run -d \
        --name "${rag_milvus_container}" \
        --network "${rag_network}" \
        --network-alias milvus \
        -p 127.0.0.1::19530 \
        -e ETCD_ENDPOINTS=etcd:2379 \
        -e MINIO_ADDRESS=minio:9000 \
        -e MINIO_ACCESS_KEY_ID=minio \
        -e MINIO_SECRET_ACCESS_KEY=minio123 \
        -e MINIO_BUCKET_NAME=recsys-milvus-ci \
        milvusdb/milvus:v2.6.18 \
        milvus run standalone >/dev/null || return 1
      rag_minio_address="$(docker port "${rag_minio_container}" 9000/tcp)" || return 1
      rag_milvus_address="$(docker port "${rag_milvus_container}" 19530/tcp)" || return 1
    }

    # Keep the ephemeral services recoverable on every exit path, including
    # readiness and port-discovery failures before pytest starts.
    trap rag_cleanup EXIT
    if ! rag_start_services; then
      rag_logs
      exit 1
    fi
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
      rag_logs
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
