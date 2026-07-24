#!/usr/bin/env bash
set -euo pipefail

component="${1:?component is required}"
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

cleanup_port_forwards() {
  local pid
  for pid in "${kfp_port_forward_pids[@]:-}"; do
    kill "${pid}" >/dev/null 2>&1 || true
  done
}
trap cleanup_port_forwards EXIT

image() {
  printf '%s/%s:%s' "${image_registry}" "$1" "${image_tag}"
}

wait_for_local_port() {
  local port="$1"
  local label="$2"
  for _ in $(seq 1 60); do
    if (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for ${label} on 127.0.0.1:${port}" >&2
  return 1
}

kfp_endpoint_for_upload() {
  local endpoint="${KFP_ENDPOINT:-http://ml-pipeline.kubeflow.svc.cluster.local:8888}"
  local local_port="${KFP_LOCAL_PORT:-18888}"
  local log_dir="${JENKINS_HOME:-/tmp}/ci-tmp"
  local log_path="${log_dir}/recsys-kfp-upload-port-forward.log"
  mkdir -p "${log_dir}"

  if [[ "${endpoint}" != *".svc.cluster.local"* ]]; then
    printf '%s\n' "${endpoint}"
    return 0
  fi

  # Close the deployment lock descriptor in the background child. Otherwise a
  # port-forward started from with_file_lock keeps flock held after this shell
  # exits and deadlocks the next rollout stage.
  kubectl port-forward -n "${namespace_kubeflow}" svc/ml-pipeline "${local_port}:8888" >"${log_path}" 2>&1 9>&- &
  kfp_port_forward_pids+=("$!")
  wait_for_local_port "${local_port}" "Kubeflow Pipelines upload endpoint" || {
    cat "${log_path}" >&2 || true
    return 1
  }
  printf 'http://127.0.0.1:%s\n' "${local_port}"
}

local_model_store_endpoint() {
  local endpoint="$1"
  local local_port="${MODEL_STORE_LOCAL_PORT:-19000}"
  local log_dir="${JENKINS_HOME:-/tmp}/ci-tmp"
  local log_path="${log_dir}/recsys-model-store-port-forward.log"
  mkdir -p "${log_dir}"

  if [[ -z "${endpoint}" || "${endpoint}" != *".svc.cluster.local"* ]]; then
    local_model_store_endpoint_result="${endpoint}"
    return 0
  fi

  kubectl port-forward -n "${namespace_mlops}" svc/minio "${local_port}:9000" >"${log_path}" 2>&1 9>&- &
  kfp_port_forward_pids+=("$!")
  wait_for_local_port "${local_port}" "model store endpoint" || {
    cat "${log_path}" >&2 || true
    return 1
  }
  local_model_store_endpoint_result="http://127.0.0.1:${local_port}"
}

configure_local_model_store_endpoint() {
  local endpoint="${MODEL_STORE_ENDPOINT:-${MLFLOW_S3_ENDPOINT_URL:-${MINIO_ENDPOINT:-}}}"
  local_model_store_endpoint "${endpoint}"
  endpoint="${local_model_store_endpoint_result}"
  if [[ -n "${endpoint}" ]]; then
    export MODEL_STORE_ENDPOINT="${endpoint}"
    export MLFLOW_S3_ENDPOINT_URL="${endpoint}"
    export MINIO_ENDPOINT="${endpoint}"
  fi
}

resource_exists() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  kubectl get "${kind}/${name}" -n "${namespace}" >/dev/null 2>&1
}

verify_workload_image() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  local expected_image="$4"

  if ! resource_exists "${kind}" "${name}" "${namespace}"; then
    echo "Skipping image check for ${kind}/${name} in ${namespace}; resource is not installed in this environment."
    return 0
  fi

  local images
  images="$(kubectl get "${kind}/${name}" -n "${namespace}" -o jsonpath='{range .spec.template.spec.containers[*]}{.image}{"\n"}{end}')"
  echo "Current images for ${kind}/${name} in ${namespace}:"
  printf '%s\n' "${images}"
  if [[ -n "${expected_image}" ]] && ! grep -Fq "${expected_image}" <<<"${images}"; then
    echo "Expected image ${expected_image} was not found on ${kind}/${name} in ${namespace}." >&2
    exit 1
  fi
}

wait_rollout_if_exists() {
  local kind="$1"
  local name="$2"
  local namespace="$3"

  if ! resource_exists "${kind}" "${name}" "${namespace}"; then
    echo "Skipping rollout wait for ${kind}/${name} in ${namespace}; resource is not installed in this environment."
    return 0
  fi

  kubectl rollout status "${kind}/${name}" -n "${namespace}" --timeout="${timeout}"
}

verify_and_wait_workload() {
  local kind="$1"
  local name="$2"
  local namespace="$3"
  local expected_image="$4"

  verify_workload_image "${kind}" "${name}" "${namespace}" "${expected_image}"
  wait_rollout_if_exists "${kind}" "${name}" "${namespace}"
}

run_node_rebalance_if_enabled() {
  if [[ "${run_node_rebalance}" == "0" || "${run_node_rebalance}" == "false" ]]; then
    echo "Skipping node rebalance because RUN_NODE_REBALANCE=${run_node_rebalance}."
    return 0
  fi

  bash infra/k8s/scripts/rebalance_ml_node_pool.sh
  if [[ "${validate_node_rebalance}" == "1" || "${validate_node_rebalance}" == "true" ]]; then
    bash jenkins/scripts/validate_node_rebalance.sh
  fi
}

with_file_lock() {
  local lock_file="$1"
  shift

  if command -v flock >/dev/null 2>&1; then
    (
      # with_file_lock runs the deployment in a subshell, so its background
      # tunnel PIDs are not visible to the parent shell's EXIT trap.
      trap cleanup_port_forwards EXIT
      flock 9
      "$@"
    ) 9>"${lock_file}"
    return
  fi

  echo "flock is not available; running ${*} without a process lock."
  "$@"
}

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

load_secret_env_if_unset() {
  local namespace="$1"
  local secret_name="$2"
  shift 2

  if ! kubectl get secret "${secret_name}" -n "${namespace}" >/dev/null 2>&1; then
    echo "Secret ${secret_name} in namespace ${namespace} was not found; continuing with existing environment."
    return 0
  fi

  local key encoded value loaded=0
  for key in "$@"; do
    if [[ -n "${!key:-}" ]]; then
      continue
    fi
    encoded="$(kubectl get secret "${secret_name}" -n "${namespace}" -o "jsonpath={.data.${key}}" 2>/dev/null || true)"
    if [[ -z "${encoded}" ]]; then
      continue
    fi
    value="$(printf '%s' "${encoded}" | base64 -d)"
    export "${key}=${value}"
    loaded=1
  done

  if [[ "${loaded}" == "1" ]]; then
    echo "Loaded model store environment from secret ${secret_name} in namespace ${namespace} (values hidden)."
  else
    echo "No additional model store environment keys were loaded from secret ${secret_name}; using existing environment."
  fi
}

verify_rayjob_image() {
  local expected_image="$1"
  local rayjob_name="${RAYJOB_NAME:-recsys-bst-ray-tune}"

  if ! resource_exists "rayjob" "${rayjob_name}" "${namespace_kubeflow}"; then
    echo "Skipping RayJob image check for ${rayjob_name}; RayJob is not installed in ${namespace_kubeflow}."
    return 0
  fi

  local images
  images="$(kubectl get "rayjob/${rayjob_name}" -n "${namespace_kubeflow}" -o jsonpath='{.spec.rayClusterSpec.headGroupSpec.template.spec.containers[*].image}{" "}{.spec.rayClusterSpec.workerGroupSpecs[*].template.spec.containers[*].image}')"
  echo "Current RayJob images for ${rayjob_name}: ${images}"
  if ! grep -Fq "${expected_image}" <<<"${images}"; then
    echo "Expected RayJob image ${expected_image} was not found on ${rayjob_name}." >&2
    exit 1
  fi
}

deploy_data_platform_unlocked() {
  # This bootstrap Job is idempotent, but Kubernetes Job pod templates are
  # immutable. Recreate it before Helm changes image or pull-policy fields.
  kubectl delete job init-data-platform-minio \
    --namespace "${namespace_data}" \
    --ignore-not-found \
    --wait=true

  helm upgrade --install recsys-data-platform infra/helm/recsys-data-platform \
    --namespace "${namespace_data}" \
    --create-namespace \
    --reuse-values \
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

deploy_api_unlocked() {
  local helm_args=(
    upgrade --install recsys-serving infra/helm/recsys-serving
    --namespace "${namespace_kserve}"
    --create-namespace
    --reuse-values
    --timeout "${timeout}"
    --set "api.namespace.name=${namespace_api}"
    --set "api.image=$(image recsys-api-serving)"
    --set "api.imagePullPolicy=Always"
    --set "featureApi.image=$(image recsys-api-serving)"
    --set "featureApi.imagePullPolicy=Always"
    --set "shadow.enabled=false"
    --set "shadow.samplePercent=100"
    --set "shadow.timeoutMs=1000"
    --set "shadow.queueSize=100"
    --set "shadow.maxConcurrency=4"
  )
  if [[ -n "${API_ROLLOUT_MAX_SURGE:-}" ]]; then
    helm_args+=(--set "api.rollout.maxSurge=${API_ROLLOUT_MAX_SURGE}")
  fi
  if [[ -n "${API_ROLLOUT_MAX_UNAVAILABLE:-}" ]]; then
    helm_args+=(--set "api.rollout.maxUnavailable=${API_ROLLOUT_MAX_UNAVAILABLE}")
  fi

  helm "${helm_args[@]}"
  verify_and_wait_workload "deployment" "recsys-online-feature-api" "${namespace_api}" "$(image recsys-api-serving)"
  verify_and_wait_workload "deployment" "recsys-api-serving" "${namespace_api}" "$(image recsys-api-serving)"
}

deploy_api() {
  with_file_lock "/tmp/recsys-serving-helm.lock" deploy_api_unlocked
}

deploy_demo_security_unlocked() {
  helm upgrade --install recsys-security infra/helm/recsys-security \
    --namespace recsys-security \
    --create-namespace \
    --reuse-values \
    --wait \
    --timeout "${timeout}"
}

collect_demo_diagnostics() {
  kubectl get pods,ingress,certificate,externalsecret -n "${namespace_demo}" -o wide \
    >.demo-web/resources.txt 2>&1 || true
  kubectl get events -n "${namespace_demo}" --sort-by=.lastTimestamp \
    >.demo-web/events.txt 2>&1 || true
  kubectl describe deploy/recsys-demo-api -n "${namespace_demo}" \
    >.demo-web/backend-describe.txt 2>&1 || true
  kubectl logs deploy/recsys-demo-api -n "${namespace_demo}" --all-containers --tail=300 \
    >.demo-web/backend.log 2>&1 || true
  helm history "${DEMO_WEB_RELEASE:-recsys-demo-web}" -n "${namespace_demo}" \
    >.demo-web/helm-history.txt 2>&1 || true
}

rollback_demo_release() {
  local release="$1"
  local previous_revision="$2"
  if [[ -n "${previous_revision}" ]]; then
    helm rollback "${release}" "${previous_revision}" -n "${namespace_demo}" --wait --timeout "${timeout}"
  elif helm status "${release}" -n "${namespace_demo}" >/dev/null 2>&1; then
    helm uninstall "${release}" -n "${namespace_demo}" --wait --timeout "${timeout}"
  fi
}

migrate_demo_ingress_split() {
  local legacy_api_path=""
  local frontend_upstream="recsys-demo-web.${namespace_demo}.svc.cluster.local"

  # ingress-nginx rejects two resources that temporarily claim the same
  # host/path. Older releases kept /, /api, /healthz and /ready in one
  # Ingress, while the mesh-safe chart needs separate frontend/backend
  # Ingresses so each location gets the correct upstream Host header.
  legacy_api_path="$(kubectl get ingress/recsys-demo-web -n "${namespace_demo}" \
    -o jsonpath='{range .spec.rules[*].http.paths[*]}{.path}{"\n"}{end}' 2>/dev/null \
    | grep -Fx '/api' || true)"
  if [[ -z "${legacy_api_path}" ]] || kubectl get ingress/recsys-demo-api \
    -n "${namespace_demo}" >/dev/null 2>&1; then
    return 0
  fi

  kubectl patch ingress/recsys-demo-web -n "${namespace_demo}" --type=json \
    --patch "[\
      {\"op\":\"add\",\"path\":\"/metadata/annotations/nginx.ingress.kubernetes.io~1upstream-vhost\",\"value\":\"${frontend_upstream}\"},\
      {\"op\":\"replace\",\"path\":\"/spec/rules/0/http/paths\",\"value\":[{\"path\":\"/\",\"pathType\":\"Prefix\",\"backend\":{\"service\":{\"name\":\"recsys-demo-web\",\"port\":{\"number\":80}}}}]}\
    ]"
}

deploy_demo_web_unlocked() {
  mkdir -p .demo-web
  local release="${DEMO_WEB_RELEASE:-recsys-demo-web}"
  local previous_revision=""

  # The demo API must reach source Postgres while the internal inference and
  # feature APIs remain covered by the existing mesh authorization rules.
  with_file_lock "/tmp/recsys-security-helm.lock" deploy_demo_security_unlocked

  previous_revision="$(helm history "${release}" -n "${namespace_demo}" -o json 2>/dev/null \
    | python3 -c 'import json,sys; rows=json.load(sys.stdin); deployed=[row for row in rows if row.get("status")=="deployed"]; print(deployed[-1]["revision"] if deployed else "")' 2>/dev/null || true)"
  printf '%s\n' "${previous_revision}" >.demo-web/previous-revision

  migrate_demo_ingress_split

  if ! helm upgrade --install "${release}" infra/helm/recsys-demo-web \
    --namespace "${namespace_demo}" \
    --create-namespace \
    -f infra/helm/recsys-demo-web/values-gcp.yaml \
    --atomic \
    --cleanup-on-fail \
    --wait \
    --history-max 10 \
    --timeout "${timeout}" \
    --set "frontend.image=$(image recsys-demo-web)" \
    --set "backend.image=$(image recsys-demo-api)"; then
    collect_demo_diagnostics
    rollback_demo_release "${release}" "${previous_revision}"
    echo "Demo web rollout failed; production release was restored." >&2
    return 1
  fi

  verify_and_wait_workload "deployment" "recsys-demo-web" "${namespace_demo}" "$(image recsys-demo-web)"
  verify_and_wait_workload "deployment" "recsys-demo-api" "${namespace_demo}" "$(image recsys-demo-api)"
  kubectl wait --for=condition=Ready externalsecret/recsys-demo-web-db \
    -n "${namespace_demo}" --timeout="${timeout}"
  for _ in $(seq 1 60); do
    kubectl get certificate/recsys-web-tls -n "${namespace_demo}" >/dev/null 2>&1 && break
    sleep 2
  done
  kubectl wait --for=condition=Ready certificate/recsys-web-tls \
    -n "${namespace_demo}" --timeout="${timeout}"
  kubectl wait --for=jsonpath='{.status.loadBalancer.ingress[0].ip}' ingress/recsys-demo-web \
    -n "${namespace_demo}" --timeout="${timeout}"

  if ! bash jenkins/scripts/demo_web_smoke.sh; then
    collect_demo_diagnostics
    rollback_demo_release "${release}" "${previous_revision}"
    echo "Demo web smoke failed; production release was rolled back." >&2
    return 1
  fi
}

deploy_demo_web() {
  with_file_lock "/tmp/recsys-demo-web-helm.lock" deploy_demo_web_unlocked
}

deploy_mlflow() {
  kubectl delete job/minio-create-mlflow-bucket -n "${namespace_mlops}" --ignore-not-found=true
  helm upgrade --install recsys-mlflow infra/helm/mlflow-stack \
    --namespace "${namespace_mlops}" \
    --create-namespace \
    --reuse-values \
    --timeout "${timeout}" \
    --wait \
    --set "nodeSelector.recsys\\.ai/pool=ml-system" \
    --set "tolerations[0].key=recsys.ai/workload" \
    --set "tolerations[0].operator=Equal" \
    --set "tolerations[0].value=ml-system" \
    --set "tolerations[0].effect=NoSchedule" \
    --set "minio.resources.requests.cpu=100m" \
    --set "minio.resources.requests.memory=512Mi" \
    --set "postgres.resources.requests.cpu=100m" \
    --set "postgres.resources.requests.memory=256Mi" \
    --set "mlflow.resources.requests.cpu=100m" \
    --set "mlflow.resources.requests.memory=512Mi" \
    --set "mlflow.image=$(image recsys-mlflow)" \
    --set "mlflow.imagePullPolicy=Always"
  verify_and_wait_workload "deployment" "mlflow" "${namespace_mlops}" "$(image recsys-mlflow)"
  wait_rollout_if_exists "deployment" "minio" "${namespace_mlops}"
  wait_rollout_if_exists "deployment" "postgres" "${namespace_mlops}"
}

deploy_training_refs() {
  local training_image
  local spark_image
  local dataflow_image

  training_image="$(image recsys-mlops-training)"
  spark_image="$(image recsys-mlops-spark)"
  dataflow_image="$(image recsys-dataflow-cli)"

  KFP_ENDPOINT="$(kfp_endpoint_for_upload)" \
    RECSYS_PIPELINE_IMAGE="${training_image}" \
    RECSYS_RAY_IMAGE="${training_image}" \
    RECSYS_SPARK_IMAGE="${spark_image}" \
    bash jenkins/scripts/kubeflow_pipeline_cicd.sh

  deploy_data_platform --set "images.dataflowCli=${dataflow_image}"
  verify_data_platform_config_image "DATAFLOW_IMAGE" "${dataflow_image}"
  verify_and_wait_workload "deployment" "realtime-event-producer" "${namespace_data}" "${dataflow_image}"

  echo "Training CI/CD built pullable ML images, compiled and uploaded the Kubeflow package, and deployed the trigger runtime image."
}

deploy_kserve_unlocked() {
  load_secret_env_if_unset "${namespace_kubeflow}" "${MLOPS_RUNTIME_SECRET_NAME:-recsys-mlops-runtime}" \
    AWS_ACCESS_KEY_ID \
    AWS_SECRET_ACCESS_KEY \
    AWS_DEFAULT_REGION \
    MINIO_ENDPOINT \
    MINIO_ROOT_USER \
    MINIO_ROOT_PASSWORD \
    MLFLOW_S3_ENDPOINT_URL \
    MODEL_STORE_ENDPOINT \
    MODEL_STORE_BUCKET \
    MODEL_STORE_PREFIX

  echo "KServe CI/CD validates the promoted Triton model manifest only."
  echo "Production model deployment is handled by the RecSys-KServe-Model-CD job after Kubeflow promotion."
  configure_local_model_store_endpoint
  RECSYS_MODEL_CD_ATOMIC="${RECSYS_MODEL_CD_ATOMIC:-0}" \
    uv run --no-project --with boto3 python jenkins/scripts/model_cd.py \
    --manifest-uri "${promotion_manifest_uri}" \
    --output-dir .model-cd \
    --timeout "${timeout}"
}

deploy_kserve_model_cd_unlocked() {
  load_secret_env_if_unset "${namespace_kubeflow}" "${MLOPS_RUNTIME_SECRET_NAME:-recsys-mlops-runtime}" \
    AWS_ACCESS_KEY_ID \
    AWS_SECRET_ACCESS_KEY \
    AWS_DEFAULT_REGION \
    MINIO_ENDPOINT \
    MINIO_ROOT_USER \
    MINIO_ROOT_PASSWORD \
    MLFLOW_S3_ENDPOINT_URL \
    MODEL_STORE_ENDPOINT \
    MODEL_STORE_BUCKET \
    MODEL_STORE_PREFIX

  configure_local_model_store_endpoint
  local model_cd_args=(
    --manifest-uri "${promotion_manifest_uri}"
    --stage "${MODEL_CD_STAGE:-deploy}"
    --candidate-weight-percent "${AB_CANDIDATE_WEIGHT_PERCENT:-10}"
    --output-dir .model-cd
    --timeout "${timeout}"
  )
  [[ -n "${CONTROL_MANIFEST_URI:-}" ]] && model_cd_args+=(--control-manifest-uri "${CONTROL_MANIFEST_URI}")
  [[ -n "${CANDIDATE_MANIFEST_URI:-}" ]] && model_cd_args+=(--candidate-manifest-uri "${CANDIDATE_MANIFEST_URI}")
  [[ -n "${AB_EXPERIMENT_ID:-}" ]] && model_cd_args+=(--experiment-id "${AB_EXPERIMENT_ID}")
  [[ -n "${PROMETHEUS_URL:-}" ]] && model_cd_args+=(--prometheus-url "${PROMETHEUS_URL}")
  [[ -n "${AB_GATE_WINDOW:-}" ]] && model_cd_args+=(--gate-window "${AB_GATE_WINDOW}")
  [[ -n "${AB_MIN_SAMPLES:-}" ]] && model_cd_args+=(--min-samples "${AB_MIN_SAMPLES}")
  if [[ "${MODEL_CD_APPLY:-1}" == "1" ]]; then
    model_cd_args+=(--apply)
  fi
  RECSYS_MODEL_CD_ATOMIC="${RECSYS_MODEL_CD_ATOMIC:-0}" \
    uv run --no-project --with boto3 python jenkins/scripts/model_cd.py \
    "${model_cd_args[@]}"
}

deploy_kserve() {
  with_file_lock "/tmp/recsys-serving-helm.lock" deploy_kserve_unlocked
}

deploy_kserve_model_cd() {
  with_file_lock "/tmp/recsys-serving-helm.lock" deploy_kserve_model_cd_unlocked
}

reconcile_rollout_jenkins_jobs() {
  local values_file="$1"
  local jenkins_url="${JENKINS_URL:-http://recsys-jenkins.${namespace_ci}.svc.cluster.local:8080}"
  local admin_secret="${JENKINS_ADMIN_SECRET_NAME:-recsys-jenkins-admin}"
  local seed_dir="${JENKINS_HOME:-/tmp}/ci-tmp"
  local seed_script="${seed_dir}/recsys-rollout-seed.groovy"
  local username password crumb_json crumb_header cookie_file
  jenkins_url="${jenkins_url%/}"

  if [[ "${RECONCILE_JENKINS_ROLLOUT_JOBS:-1}" == "0" ]]; then
    echo "Skipping Jenkins rollout job reconciliation."
    return 0
  fi

  # Update the init ConfigMap without rolling the Jenkins pod, then execute the
  # idempotent seed script through Jenkins. This creates/updates the rollout
  # proof job and migrates Model-CD to Pipeline-from-SCM in the same deploy.
  helm template recsys-ci infra/helm/recsys-ci \
    --namespace "${namespace_ci}" \
    -f "${values_file}" \
    --set "namespace.name=${namespace_ci}" \
    --show-only templates/jenkins-init-configmap.yaml \
    | kubectl apply -f -

  mkdir -p "${seed_dir}"
  kubectl get configmap recsys-jenkins-init -n "${namespace_ci}" \
    -o 'jsonpath={.data.zz-seed-cicd-views\.groovy}' >"${seed_script}"
  username="$(kubectl get secret "${admin_secret}" -n "${namespace_ci}" -o 'jsonpath={.data.username}' | base64 -d)"
  password="$(kubectl get secret "${admin_secret}" -n "${namespace_ci}" -o 'jsonpath={.data.password}' | base64 -d)"
  cookie_file="${seed_script}.cookie"
  crumb_json="$(curl -fsS -c "${cookie_file}" -u "${username}:${password}" "${jenkins_url}/crumbIssuer/api/json")"
  crumb_header="$(python3 -c 'import json,sys; p=json.load(sys.stdin); print("{}: {}".format(p["crumbRequestField"], p["crumb"]))' <<<"${crumb_json}")"
  if ! curl -fsS \
    -u "${username}:${password}" \
    -b "${cookie_file}" \
    -H "${crumb_header}" \
    --data-urlencode "script@${seed_script}" \
    "${jenkins_url}/scriptText" >/dev/null; then
    rm -f "${cookie_file}" "${seed_script}"
    return 1
  fi
  rm -f "${cookie_file}" "${seed_script}"
  echo "Reconciled RecSys-Progressive-Rollout-CICD and SCM-backed RecSys-KServe-Model-CD without restarting Jenkins."
}

deploy_rollout_watcher() {
  local watcher_image
  local values_file
  watcher_image="$(image recsys-mlops-training)"
  values_file="${ROLLOUT_CI_VALUES_FILE:-infra/helm/recsys-ci/values-gke.yaml}"

  reconcile_rollout_jenkins_jobs "${values_file}"

  # Render and apply only the watcher resource. Upgrading the complete recsys-ci
  # release from a build running inside Jenkins would restart its own controller.
  helm template recsys-ci infra/helm/recsys-ci \
    --namespace "${namespace_ci}" \
    -f "${values_file}" \
    --set "namespace.name=${namespace_ci}" \
    --set "modelRolloutWatcher.enabled=true" \
    --set "modelRolloutWatcher.image=${watcher_image}" \
    --set "modelRolloutWatcher.imagePullPolicy=Always" \
    --set "modelRolloutWatcher.autoProgressiveEnabled=true" \
    --set-string 'modelRolloutWatcher.progressiveWeights=10\,25\,50' \
    --show-only templates/model-rollout-watcher.yaml \
    | kubectl apply -f -

  verify_and_wait_workload \
    "deployment" \
    "recsys-model-rollout-watcher" \
    "${namespace_ci}" \
    "${watcher_image}"
  echo "Progressive rollout watcher deployed from immutable image ${watcher_image}."
}

deploy_drift() {
  deploy_data_platform --set "images.dataflowCli=$(image recsys-dataflow-cli)"
  if [[ -d infra/knative/recsys-drift ]]; then
    kubectl apply -k infra/knative/recsys-drift
  else
    echo "No infra/knative/recsys-drift manifests yet; deployed drift-capable dataflow image only."
  fi
}

deploy_analytics() {
  local secret_create=true
  local external_secret_enabled=false
  if [[ "${ANALYTICS_EXTERNAL_SECRET_ENABLED:-1}" == "1" ]]; then
    secret_create=false
    external_secret_enabled=true
  elif [[ "${ANALYTICS_ALLOW_DEV_SECRETS:-0}" != "1" ]]; then
    if ! kubectl get secret recsys-analytics-secret -n "${namespace_analytics}" >/dev/null 2>&1; then
      echo "Secret recsys-analytics-secret must be provisioned in ${namespace_analytics} before production deploy." >&2
      exit 2
    fi
    secret_create=false
  fi

  deploy_data_platform \
    --set "images.airflow=$(image recsys-airflow)" \
    --set "images.analyticsSpark=$(image recsys-analytics-spark)" \
    --set "images.analyticsDbt=$(image recsys-analytics-dbt)"
  verify_and_wait_workload "deployment" "airflow-scheduler" "${namespace_data}" "$(image recsys-airflow)"

  helm upgrade --install recsys-analytics infra/helm/recsys-analytics \
    --namespace "${namespace_analytics}" \
    --create-namespace \
    --reuse-values \
    --timeout "${timeout}" \
    --wait \
    --set "namespace=${namespace_analytics}" \
    --set "secrets.create=${secret_create}" \
    --set "externalSecret.enabled=${external_secret_enabled}" \
    --set "images.pullPolicy=Always" \
    --set "images.spark=$(image recsys-analytics-spark)" \
    --set "images.dbt=$(image recsys-analytics-dbt)" \
    --set "images.superset=$(image recsys-analytics-superset)"

  verify_and_wait_workload "deployment" "recsys-analytics-superset" "${namespace_analytics}" "$(image recsys-analytics-superset)"
  wait_rollout_if_exists "deployment" "recsys-analytics-trino" "${namespace_analytics}"
  wait_rollout_if_exists "deployment" "recsys-analytics-redis" "${namespace_analytics}"
  wait_rollout_if_exists "statefulset" "recsys-analytics-catalog-postgres" "${namespace_analytics}"
  wait_rollout_if_exists "statefulset" "recsys-analytics-superset-postgres" "${namespace_analytics}"
}

deploy_all() {
  local training_image
  local spark_image
  local dataflow_image
  local airflow_image
  local kafka_connect_image
  local flink_image

  training_image="$(image recsys-mlops-training)"
  spark_image="$(image recsys-mlops-spark)"
  dataflow_image="$(image recsys-dataflow-cli)"
  airflow_image="$(image recsys-airflow)"
  kafka_connect_image="$(image recsys-kafka-connect)"
  flink_image="$(image recsys-flink)"

  KFP_ENDPOINT="$(kfp_endpoint_for_upload)" \
    RECSYS_PIPELINE_IMAGE="${training_image}" \
    RECSYS_RAY_IMAGE="${training_image}" \
    RECSYS_SPARK_IMAGE="${spark_image}" \
    bash jenkins/scripts/kubeflow_pipeline_cicd.sh

  deploy_data_platform \
    --set "images.dataflowCli=${dataflow_image}" \
    --set "images.spark=$(image recsys-spark)" \
    --set "images.airflow=${airflow_image}" \
    --set "images.kafkaConnect=${kafka_connect_image}" \
    --set "images.flink=${flink_image}" \
    --set "observability.retrainPsiThreshold=${RETRAIN_PSI_THRESHOLD:-0.15}"

  verify_data_platform_config_image "DATAFLOW_IMAGE" "${dataflow_image}"
  verify_data_platform_config_image "SPARK_IMAGE" "$(image recsys-spark)"
  verify_data_platform_config_image "FLINK_IMAGE" "${flink_image}"
  verify_and_wait_workload "deployment" "airflow-webserver" "${namespace_data}" "${airflow_image}"
  verify_and_wait_workload "deployment" "airflow-scheduler" "${namespace_data}" "${airflow_image}"
  verify_and_wait_workload "deployment" "kafka-connect" "${namespace_data}" "${kafka_connect_image}"
  verify_and_wait_workload "deployment" "realtime-event-producer" "${namespace_data}" "${dataflow_image}"
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

case "${component}" in
  materialize|spark_batch|dp1|dp2|dp3|stream_offline|stream_online)
    case "${component}" in
      materialize)
        deploy_data_platform --set "images.dataflowCli=$(image recsys-dataflow-cli)"
        verify_data_platform_config_image "DATAFLOW_IMAGE" "$(image recsys-dataflow-cli)"
        verify_and_wait_workload "deployment" "realtime-event-producer" "${namespace_data}" "$(image recsys-dataflow-cli)"
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
          --set "images.dataflowCli=$(image recsys-dataflow-cli)" \
          --set "images.airflow=$(image recsys-airflow)" \
          --set "images.kafkaConnect=$(image recsys-kafka-connect)"
        verify_data_platform_config_image "SPARK_IMAGE" "$(image recsys-spark)"
        verify_data_platform_config_image "DATAFLOW_IMAGE" "$(image recsys-dataflow-cli)"
        verify_and_wait_workload "deployment" "realtime-event-producer" "${namespace_data}" "$(image recsys-dataflow-cli)"
        verify_and_wait_workload "deployment" "airflow-webserver" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "airflow-scheduler" "${namespace_data}" "$(image recsys-airflow)"
        verify_and_wait_workload "deployment" "kafka-connect" "${namespace_data}" "$(image recsys-kafka-connect)"
        ;;
      dp3)
        deploy_data_platform \
          --set "images.spark=$(image recsys-spark)" \
          --set "images.dataflowCli=$(image recsys-dataflow-cli)" \
          --set "images.airflow=$(image recsys-airflow)"
        verify_data_platform_config_image "SPARK_IMAGE" "$(image recsys-spark)"
        verify_data_platform_config_image "DATAFLOW_IMAGE" "$(image recsys-dataflow-cli)"
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
        deploy_data_platform --set "images.flink=$(image recsys-flink)" --set "images.dataflowCli=$(image recsys-dataflow-cli)"
        verify_data_platform_config_image "FLINK_IMAGE" "$(image recsys-flink)"
        verify_data_platform_config_image "DATAFLOW_IMAGE" "$(image recsys-dataflow-cli)"
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
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac
