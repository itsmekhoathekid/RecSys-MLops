#!/usr/bin/env bash
set -euo pipefail

component="${1:?component is required}"
if [[ -n "${CI_TMP_ROOT:-}" ]]; then
  ci_profile="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["ciProfile"])' \
      <<<"$(python3 jenkins/python/configuration.py component "${component}")"
  )"
  component_environment="${CI_TMP_ROOT}/envs/${ci_profile}"
  if [[ -x "${component_environment}/bin/python" ]]; then
    export UV_PROJECT_ENVIRONMENT="${component_environment}"
  fi
fi

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/gcp.sh
source jenkins/scripts/lib/helm.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/lib/kubernetes.sh
source jenkins/scripts/lib/port_forward.sh
source jenkins/scripts/deploy/transaction.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/deploy/database.sh
source jenkins/scripts/deploy/rollout.sh
source jenkins/scripts/deploy/data_platform.sh
source jenkins/scripts/deploy/ml_platform.sh
source jenkins/scripts/deploy/serving.sh
source jenkins/scripts/deploy/demo.sh
source jenkins/scripts/deploy/analytics.sh
source jenkins/scripts/test/runtime.sh
source jenkins/scripts/test/data_platform.sh
source jenkins/scripts/test/ml_platform.sh
source jenkins/scripts/test/serving.sh
source jenkins/scripts/test/demo.sh
source jenkins/scripts/test/analytics.sh
source jenkins/scripts/test/dispatch.sh

image_registry="${IMAGE_PULL_REGISTRY:-${IMAGE_REGISTRY:-localhost:5001/recsys}}"
image_registry="${image_registry%/}"
image_tag="${IMAGE_TAG:-${GIT_COMMIT:-}}"
namespace_data="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
namespace_api="${API_NAMESPACE:-api-serving}"
namespace_kserve="${KSERVE_NAMESPACE:-kserve-triton-inference}"
namespace_kubeflow="${KUBEFLOW_NAMESPACE:-kubeflow}"
namespace_mlops="${MLOPS_NAMESPACE:-experiment-tracking}"
namespace_analytics="${ANALYTICS_NAMESPACE:-analytics}"
namespace_demo="${DEMO_WEB_NAMESPACE:-api-serving}"
namespace_ci="${CI_NAMESPACE:-ci}"
promotion_manifest_uri="${PROMOTION_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/latest.json}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
run_node_rebalance="${RUN_NODE_REBALANCE:-1}"
validate_node_rebalance="${VALIDATE_NODE_REBALANCE:-1}"
kfp_port_forward_pids=()
local_model_store_endpoint_result=""

if [[ -n "${JENKINS_HOME:-}" ]]; then
  export UV_CACHE_DIR="${JENKINS_UV_CACHE_DIR:-${JENKINS_HOME}/.cache/uv}"
fi

if [[ -z "${image_tag}" ]]; then
  image_tag="$(git rev-parse --short=12 HEAD)"
fi

trap component_deploy_on_exit EXIT

verify_data_platform_config_image() {
  local key="$1"
  local expected_image="$2"
  local configmap_name="recsys-data-platform-config"

  local actual_image
  actual_image="$(kubectl get configmap "${configmap_name}" -n "${namespace_data}" -o "jsonpath={.data.${key}}")"
  echo "${configmap_name}.${key}=${actual_image}"
  if [[ "${actual_image}" != "${expected_image}" ]]; then
    echo "Expected ${configmap_name}.${key}=${expected_image}, got ${actual_image}." >&2
    exit 1
  fi
}

deploy_data_platform_unlocked() {
  helm upgrade --install recsys-data-platform infra/helm/recsys-data-platform \
    --namespace "${namespace_data}" \
    --create-namespace \
    --reuse-values \
    --atomic \
    --cleanup-on-fail \
    --history-max "${HELM_HISTORY_MAX:-10}" \
    --timeout "${timeout}" \
    --wait \
    --wait-for-jobs \
    --set "images.pullPolicy=Always" \
    --set "spark.driverMemory=${SPARK_K8S_DRIVER_MEMORY:-2g}" \
    --set "spark.driverMemoryOverhead=${SPARK_K8S_DRIVER_MEMORY_OVERHEAD:-1g}" \
    --set "spark.executorMemory=${SPARK_K8S_EXECUTOR_MEMORY:-1536m}" \
    --set "spark.executorMemoryOverhead=${SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD:-1536m}" \
    --set "spark.executorInstances=${SPARK_K8S_EXECUTOR_INSTANCES:-1}" \
    --set "spark.dynamicAllocation.enabled=${SPARK_DYNAMIC_ALLOCATION_ENABLED:-false}" \
    --set "spark.dynamicAllocation.shuffleTrackingEnabled=${SPARK_DYNAMIC_ALLOCATION_SHUFFLE_TRACKING_ENABLED:-true}" \
    --set "spark.dynamicAllocation.minExecutors=${SPARK_DYNAMIC_ALLOCATION_MIN_EXECUTORS:-1}" \
    --set "spark.dynamicAllocation.initialExecutors=${SPARK_DYNAMIC_ALLOCATION_INITIAL_EXECUTORS:-1}" \
    --set "spark.dynamicAllocation.maxExecutors=${SPARK_DYNAMIC_ALLOCATION_MAX_EXECUTORS:-1}" \
    --set "spark.dynamicAllocation.executorIdleTimeout=${SPARK_DYNAMIC_ALLOCATION_EXECUTOR_IDLE_TIMEOUT:-60s}" \
    --set "spark.dynamicAllocation.schedulerBacklogTimeout=${SPARK_DYNAMIC_ALLOCATION_SCHEDULER_BACKLOG_TIMEOUT:-1s}" \
    --set "spark.dynamicAllocation.sustainedSchedulerBacklogTimeout=${SPARK_DYNAMIC_ALLOCATION_SUSTAINED_BACKLOG_TIMEOUT:-1s}" \
    --set "spark.sqlShufflePartitions=${SPARK_SQL_SHUFFLE_PARTITIONS:-16}" \
    --set-string "spark.advisoryPartitionSizeBytes=${SPARK_ADVISORY_PARTITION_SIZE_BYTES:-134217728}" \
    --set "drift.currentRoot=${OFFLINE_FEATURE_DRIFT_CURRENT_ROOT:-s3a://recsys-offline-feature-store/monitoring/offline_feature_drift/current_snapshot}" \
    --set "kafka.topicPartitions=${KAFKA_TOPIC_PARTITIONS:-4}" \
    --set "flinkTaskManager.replicas=${FLINK_TASKMANAGER_REPLICAS:-2}" \
    --set "flinkTaskManager.resources.requests.cpu=${FLINK_TASKMANAGER_REQUEST_CPU:-500m}" \
    --set "flinkTaskManager.resources.requests.memory=${FLINK_TASKMANAGER_REQUEST_MEMORY:-6Gi}" \
    --set "flinkTaskManager.resources.limits.cpu=${FLINK_TASKMANAGER_LIMIT_CPU:-2}" \
    --set "flinkTaskManager.resources.limits.memory=${FLINK_TASKMANAGER_LIMIT_MEMORY:-10Gi}" \
    --set "flink.taskSlots=${FLINK_TASK_SLOTS:-1}" \
    --set "flink.scheduler=${FLINK_SCHEDULER:-adaptive}" \
    --set "flink.disableJemalloc=${FLINK_DISABLE_JEMALLOC:-true}" \
    --set "flink.metricsPort=${FLINK_METRICS_PORT:-9249}" \
    --set "flink.taskManagerProcessMemory=${FLINK_TASKMANAGER_PROCESS_MEMORY:-6144m}" \
    --set "flink.taskManagerTaskHeapMemory=${FLINK_TASKMANAGER_TASK_HEAP_MEMORY:-3072m}" \
    --set "flink.taskManagerManagedMemory=${FLINK_TASKMANAGER_MANAGED_MEMORY:-512m}" \
    --set "flink.taskManagerJvmOverheadMax=${FLINK_TASKMANAGER_JVM_OVERHEAD_MAX:-2048m}" \
    --set "realtimeFlinkConsumer.parallelism=${FLINK_PARALLELISM:-1}" \
    --set "realtimeFlinkConsumer.redisSinkMaxEventsPerSecond=${REDIS_SINK_MAX_EVENTS_PER_SECOND:-200}" \
    --set "realtimeFlinkConsumer.postgresSinkMaxEventsPerSecond=${POSTGRES_SINK_MAX_EVENTS_PER_SECOND:-100}" \
    --set "realtimeFlinkConsumer.sinkRateLimitBurstEvents=${SINK_RATE_LIMIT_BURST_EVENTS:-25}" \
    --set "realtimeFlinkConsumer.asyncIoCapacity=${FLINK_ASYNC_IO_CAPACITY:-64}" \
    --set "realtimeFlinkConsumer.asyncIoTimeoutSeconds=${FLINK_ASYNC_IO_TIMEOUT_SECONDS:-120}" \
    --set "realtimeFlinkConsumer.postgresAsyncPoolSize=${POSTGRES_ASYNC_POOL_SIZE:-16}" \
    --set "flinkAutoscaler.enabled=${FLINK_AUTOSCALER_ENABLED:-true}" \
    --set "flinkAutoscaler.version=${FLINK_AUTOSCALER_VERSION:-1.15.0}" \
    --set "flinkAutoscaler.scalingEnabled=${FLINK_AUTOSCALER_SCALING_ENABLED:-true}" \
    --set "flinkAutoscaler.stabilizationInterval=${FLINK_AUTOSCALER_STABILIZATION_INTERVAL:-1m}" \
    --set "flinkAutoscaler.metricsWindow=${FLINK_AUTOSCALER_METRICS_WINDOW:-3m}" \
    --set "flinkAutoscaler.targetUtilization=${FLINK_AUTOSCALER_TARGET_UTILIZATION:-0.65}" \
    --set "flinkAutoscaler.utilizationMin=${FLINK_AUTOSCALER_UTILIZATION_MIN:-0.50}" \
    --set "flinkAutoscaler.utilizationMax=${FLINK_AUTOSCALER_UTILIZATION_MAX:-0.80}" \
    --set "flinkAutoscaler.catchUpDuration=${FLINK_AUTOSCALER_CATCH_UP_DURATION:-5m}" \
    --set "flinkAutoscaler.restartTime=${FLINK_AUTOSCALER_RESTART_TIME:-2m}" \
    --set "flinkAutoscaler.pipelineMaxParallelism=${FLINK_PIPELINE_MAX_PARALLELISM:-120}" \
    --set "flinkAutoscaler.vertexMinParallelism=${FLINK_AUTOSCALER_VERTEX_MIN_PARALLELISM:-1}" \
    --set "flinkAutoscaler.vertexMaxParallelism=${FLINK_AUTOSCALER_VERTEX_MAX_PARALLELISM:-4}" \
    --set "flinkAutoscaler.taskManagerHpa.enabled=${FLINK_TASKMANAGER_HPA_ENABLED:-true}" \
    --set "flinkAutoscaler.taskManagerHpa.minReplicas=${FLINK_TASKMANAGER_HPA_MIN_REPLICAS:-2}" \
    --set "flinkAutoscaler.taskManagerHpa.maxReplicas=${FLINK_TASKMANAGER_HPA_MAX_REPLICAS:-4}" \
    --set "flinkAutoscaler.taskManagerHpa.targetCpuUtilization=${FLINK_TASKMANAGER_HPA_TARGET_CPU:-65}" \
    --set "flinkAutoscaler.taskManagerHpa.scaleDownStabilizationSeconds=${FLINK_TASKMANAGER_HPA_SCALE_DOWN_STABILIZATION_SECONDS:-300}" \
    --set "streaming.watermarkDelayMinutes=${STREAM_WATERMARK_DELAY_MINUTES:-5}" \
    --set "streaming.allowedLatenessSeconds=${STREAM_ALLOWED_LATENESS_SECONDS:-3600}" \
    --set "streaming.watermarkIdlenessSeconds=${STREAM_WATERMARK_IDLENESS_SECONDS:-120}" \
    --set "streaming.watermarkAlignmentEnabled=${STREAM_WATERMARK_ALIGNMENT_ENABLED:-true}" \
    --set "streaming.watermarkAlignmentGroup=${STREAM_WATERMARK_ALIGNMENT_GROUP:-recsys-cdc}" \
    --set "streaming.watermarkAlignmentMaxDriftSeconds=${STREAM_WATERMARK_ALIGNMENT_MAX_DRIFT_SECONDS:-60}" \
    --set "streaming.watermarkAlignmentUpdateIntervalSeconds=${STREAM_WATERMARK_ALIGNMENT_UPDATE_INTERVAL_SECONDS:-5}" \
    --set "streaming.qualityWindowSeconds=${STREAM_QUALITY_WINDOW_SECONDS:-60}" \
    --set "streaming.burstThresholdEventCount=${STREAM_BURST_THRESHOLD_EVENT_COUNT:-500}" \
    --set "streaming.dropLateEvents=${STREAM_DROP_LATE_EVENTS:-true}" \
    --set "streaming.enableLateEventDlq=${STREAM_ENABLE_LATE_EVENT_DLQ:-true}" \
    --set "streaming.stateTtlSeconds=${STREAM_STATE_TTL_SECONDS:-604800}" \
    --set "streaming.dedupStateTtlSeconds=${STREAM_DEDUP_STATE_TTL_SECONDS:-86400}" \
    --set "streaming.checkpointMinPauseSeconds=${STREAM_CHECKPOINT_MIN_PAUSE_SECONDS:-10}" \
    --set "streaming.checkpointTimeoutSeconds=${STREAM_CHECKPOINT_TIMEOUT_SECONDS:-300}" \
    --set "streaming.tolerableCheckpointFailures=${STREAM_TOLERABLE_CHECKPOINT_FAILURES:-2}" \
    --set "streaming.unalignedCheckpointsEnabled=${STREAM_UNALIGNED_CHECKPOINTS_ENABLED:-true}" \
    --set "sourcePostgres.istioInject=false" \
    --set "airflowPostgres.istioInject=false" \
    --set "featurePostgres.istioInject=false" \
    --set "kafkaConnect.istioInject=false" \
    --set "redis.istioInject=false" \
    --set "flink.istioInject=false" \
    --set "realtimeFlinkConsumer.istioInject=false" \
    "$@"
}

deploy_data_platform() {
  with_file_lock "/tmp/recsys-data-platform-helm.lock" deploy_data_platform_unlocked "$@"
}

deploy_all() {
  local training_image
  local spark_image
  local data_ingestion_image
  local feature_store_image
  local drift_retrain_image
  local airflow_image
  local kafka_connect_image
  local flink_image

  training_image="$(image recsys-mlops-training)"
  spark_image="$(image recsys-mlops-spark)"
  data_ingestion_image="$(image recsys-data-ingestion)"
  feature_store_image="$(image recsys-feature-store)"
  drift_retrain_image="$(image recsys-drift-retrain)"
  airflow_image="$(image recsys-airflow)"
  kafka_connect_image="$(image recsys-kafka-connect)"
  flink_image="$(image recsys-flink)"

  local kfp_upload_state=""
  if [[ "${TX_ACTIVE}" == "1" ]]; then
    kfp_upload_state="${TX_DIR}/kfp-upload.json"
    tx_register_external kfp-version "${kfp_upload_state}"
  fi
  KFP_UPLOAD_RESULT_PATH="${kfp_upload_state}" \
    KFP_ENDPOINT="$(kfp_endpoint_for_upload)" \
    RECSYS_PIPELINE_IMAGE="${training_image}" \
    RECSYS_RAY_IMAGE="${training_image}" \
    RECSYS_SPARK_IMAGE="${spark_image}" \
    bash jenkins/scripts/kubeflow_pipeline_cicd.sh

  deploy_data_platform \
    --set "images.dataIngestion=${data_ingestion_image}" \
    --set "images.featureStore=${feature_store_image}" \
    --set "images.driftRetrain=${drift_retrain_image}" \
    --set "images.spark=$(image recsys-spark)" \
    --set "images.airflow=${airflow_image}" \
    --set "images.kafkaConnect=${kafka_connect_image}" \
    --set "images.flink=${flink_image}" \
    --set "realtimeFlinkConsumer.online.startingOffsets=${FLINK_ONLINE_STARTING_OFFSETS:-committed-offsets}" \
    --set "observability.retrainPsiThreshold=${RETRAIN_PSI_THRESHOLD:-0.15}"

  verify_data_platform_config_image "DATA_INGESTION_IMAGE" "${data_ingestion_image}"
  verify_data_platform_config_image "FEATURE_STORE_IMAGE" "${feature_store_image}"
  verify_data_platform_config_image "DRIFT_RETRAIN_IMAGE" "${drift_retrain_image}"
  verify_data_platform_config_image "SPARK_IMAGE" "$(image recsys-spark)"
  verify_data_platform_config_image "FLINK_IMAGE" "${flink_image}"
  verify_and_wait_workload "deployment" "airflow-webserver" "${namespace_data}" "${airflow_image}"
  verify_and_wait_workload "deployment" "airflow-scheduler" "${namespace_data}" "${airflow_image}"
  verify_and_wait_workload "deployment" "kafka-connect" "${namespace_data}" "${kafka_connect_image}"
  verify_and_wait_workload "deployment" "realtime-event-producer" "${namespace_data}" "${data_ingestion_image}"
  verify_and_wait_workload "deployment" "flink-jobmanager" "${namespace_data}" "${flink_image}"
  verify_and_wait_workload "deployment" "flink-taskmanager" "${namespace_data}" "${flink_image}"
  verify_and_wait_workload "deployment" "realtime-flink-offline-store" "${namespace_data}" "${flink_image}"
  verify_and_wait_workload "deployment" "realtime-flink-online-store" "${namespace_data}" "${flink_image}"

  deploy_mlflow
  deploy_api
  deploy_analytics
  deploy_rollout_watcher
  deploy_kserve_model_cd

  run_node_rebalance_if_enabled

  echo "Full RecSys CI/CD deploy completed for tag ${image_tag}."
}

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
        ;;
      spark_batch|dp2)
        deploy_data_platform --set "images.spark=$(image recsys-spark)" --set "images.airflow=$(image recsys-airflow)"
        verify_data_platform_config_image "SPARK_IMAGE" "$(image recsys-spark)"
        verify_and_wait_workload "deployment" "airflow-webserver" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "airflow-scheduler" "${namespace_data}" "$(image recsys-airflow)"
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

if recsys_is_true "${RECOVER_ONLY:-0}"; then
  if [[ "${DEPLOY_TARGET:-local}" == "gcp-production" ]]; then
    python3 jenkins/python/configuration.py validate
    gcp_verify_production_target
    gcp_verify_required_crds
  fi
  TX_STATE_ROOT="$(tx_state_root)"
  mkdir -p "${TX_STATE_ROOT}"
  tx_acquire_component_locks "${component}"
  tx_recover_component "${component}" "${TX_STATE_ROOT}"
  tx_release_component_locks
  exit 0
fi

if [[ "${DEPLOY_TARGET:-local}" == "gcp-production" ]]; then
  branch_name="${BRANCH_NAME:-${GIT_BRANCH:-}}"
  if [[ "${branch_name}" != "main" && "${branch_name}" != "origin/main" ]] \
    && ! recsys_is_true "${FORCE_DEPLOY:-0}"; then
    recsys_error "GCP production deploy requires main or FORCE_DEPLOY=true; got ${branch_name:-<empty>}"
    exit 2
  fi
  recsys_is_true "${PUBLISH_IMAGES:-0}" || {
    recsys_error "GCP production deploy requires PUBLISH_IMAGES=true"
    exit 2
  }
  gcp_production_preflight
  verify_model_store_versioning_if_required
fi

tx_begin "${component}"
tx_transition SNAPSHOT
snapshot_component_releases "${component}"
tx_transition APPLYING
database_apply_component_migration "${component}"
deploy_component_dispatch "${component}"
tx_transition VERIFYING
if [[ "${DEPLOY_TARGET:-local}" == "gcp-production" ]]; then
  if component_test_run "${component}"; then
    tx_record_health_test \
      "${component}" passed "reports/junit/gcp-${component}.xml"
  else
    tx_record_health_test \
      "${component}" failed "reports/junit/gcp-${component}.xml"
    false
  fi
fi
tx_commit
