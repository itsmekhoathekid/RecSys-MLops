#!/usr/bin/env bash

snapshot_component_releases() {
  case "$1" in
    materialize|spark_batch|dp1|dp2|dp3|stream_offline|stream_online|drift)
      tx_snapshot_helm_release recsys-data-platform "${namespace_data}"
      ;;
    training)
      tx_snapshot_helm_release recsys-data-platform "${namespace_data}"
      tx_snapshot_helm_release recsys-mlflow "${namespace_mlops}"
      ;;
    api|kserve|kserve_model_cd)
      tx_snapshot_helm_release recsys-serving "${namespace_kserve}"
      ;;
    rollout)
      :
      ;;
    analytics)
      tx_snapshot_helm_release recsys-data-platform "${namespace_data}"
      tx_snapshot_helm_release recsys-analytics "${namespace_analytics}"
      ;;
    demo_web)
      tx_snapshot_helm_release recsys-security recsys-security
      tx_snapshot_helm_release "${DEMO_WEB_RELEASE:-recsys-demo-web}" "${namespace_demo}"
      ;;
    mlflow)
      tx_snapshot_helm_release recsys-mlflow "${namespace_mlops}"
      ;;
    all)
      tx_snapshot_helm_release recsys-data-platform "${namespace_data}"
      tx_snapshot_helm_release recsys-mlflow "${namespace_mlops}"
      tx_snapshot_helm_release recsys-serving "${namespace_kserve}"
      tx_snapshot_helm_release recsys-analytics "${namespace_analytics}"
      tx_snapshot_helm_release recsys-security recsys-security
      tx_snapshot_helm_release "${DEMO_WEB_RELEASE:-recsys-demo-web}" "${namespace_demo}"
      ;;
    *)
      recsys_error "unknown component: $1"
      return 2
      ;;
  esac
}
deploy_component_dispatch() {
  local selected_component="$1"
  case "${selected_component}" in
  materialize|spark_batch|dp1|dp2|dp3|stream_offline|stream_online)
    case "${selected_component}" in
      materialize)
        deploy_data_platform --set "images.featureStore=$(image recsys-feature-store)"
        verify_data_platform_config_image "FEATURE_STORE_IMAGE" "$(image recsys-feature-store)"
        feast_registry_snapshot "$(image recsys-feature-store)"
        feast_registry_plan_apply "$(image recsys-feature-store)"
        ;;
      spark_batch|dp2)
        deploy_data_platform --set "images.spark=$(image recsys-spark)" --set "images.airflow=$(image recsys-airflow)"
        verify_data_platform_config_image "SPARK_IMAGE" "$(image recsys-spark)"
        verify_and_wait_workload "deployment" "airflow-webserver" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "airflow-scheduler" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "airflow-dag-processor" "${namespace_data}" "$(image recsys-airflow)"
        ;;
      dp1)
        deploy_data_platform \
          --set "images.spark=$(image recsys-spark)" \
          --set "images.dataIngestion=$(image recsys-data-ingestion)" \
          --set "images.airflow=$(image recsys-airflow)" \
          --set "images.kafkaConnect=$(image recsys-kafka-connect)"
        verify_data_platform_config_image "SPARK_IMAGE" "$(image recsys-spark)"
        verify_data_platform_config_image "DATA_INGESTION_IMAGE" "$(image recsys-data-ingestion)"
        verify_and_wait_workload "deployment" "realtime-event-producer" "${namespace_data}" "$(image recsys-data-ingestion)"
        verify_and_wait_workload "deployment" "airflow-webserver" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "airflow-scheduler" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "airflow-dag-processor" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "kafka-connect" "${namespace_data}" "$(image recsys-kafka-connect)"
        ;;
      dp3)
        deploy_data_platform \
          --set "images.spark=$(image recsys-spark)" \
          --set "images.featureStore=$(image recsys-feature-store)" \
          --set "images.airflow=$(image recsys-airflow)"
        verify_data_platform_config_image "SPARK_IMAGE" "$(image recsys-spark)"
        verify_data_platform_config_image "FEATURE_STORE_IMAGE" "$(image recsys-feature-store)"
        verify_and_wait_workload "deployment" "airflow-webserver" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "airflow-scheduler" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "airflow-dag-processor" "${namespace_data}" "$(image recsys-airflow)"
        ;;
      stream_offline)
        deploy_data_platform --set "images.flink=$(image recsys-flink)"
        verify_data_platform_config_image "FLINK_IMAGE" "$(image recsys-flink)"
        verify_and_wait_workload "deployment" "flink-jobmanager" "${namespace_data}" "$(image recsys-flink)"
        verify_and_wait_workload "deployment" "flink-taskmanager" "${namespace_data}" "$(image recsys-flink)"
        verify_and_wait_workload "deployment" "realtime-flink-offline-store" "${namespace_data}" "$(image recsys-flink)"
        ;;
      stream_online)
        deploy_data_platform \
          --set "images.flink=$(image recsys-flink)" \
          --set "realtimeFlinkConsumer.online.startingOffsets=${FLINK_ONLINE_STARTING_OFFSETS:-committed-offsets}"
        verify_data_platform_config_image "FLINK_IMAGE" "$(image recsys-flink)"
        verify_and_wait_workload "deployment" "flink-jobmanager" "${namespace_data}" "$(image recsys-flink)"
        verify_and_wait_workload "deployment" "flink-taskmanager" "${namespace_data}" "$(image recsys-flink)"
        verify_and_wait_workload "deployment" "realtime-flink-online-store" "${namespace_data}" "$(image recsys-flink)"
        ;;
    esac
    ;;
  training)
    deploy_training_refs
    ;;
  api)
    deploy_api
    ;;
  kserve)
    deploy_kserve
    ;;
  kserve_model_cd)
    deploy_kserve_model_cd
    ;;
  rollout)
    deploy_rollout_watcher
    ;;
  drift)
    deploy_drift
    ;;
  analytics)
    deploy_analytics
    ;;
  demo_web)
    deploy_demo_web
    ;;
  mlflow)
    deploy_mlflow
    ;;
  all)
    deploy_all
    deploy_demo_web
    ;;
  *)
    recsys_error "unknown component: ${selected_component}"
    return 2
    ;;
  esac
}
