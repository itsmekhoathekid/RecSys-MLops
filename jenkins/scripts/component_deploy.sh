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
promotion_manifest_uri="${PROMOTION_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/latest.json}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
run_node_rebalance="${RUN_NODE_REBALANCE:-1}"
validate_node_rebalance="${VALIDATE_NODE_REBALANCE:-1}"
kfp_port_forward_pids=()

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
  local log_path="/tmp/recsys-kfp-upload-port-forward.log"

  if [[ "${endpoint}" != *".svc.cluster.local"* ]]; then
    printf '%s\n' "${endpoint}"
    return 0
  fi

  kubectl port-forward -n "${namespace_kubeflow}" svc/ml-pipeline "${local_port}:8888" >"${log_path}" 2>&1 &
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
  local log_path="/tmp/recsys-model-store-port-forward.log"

  if [[ -z "${endpoint}" || "${endpoint}" != *".svc.cluster.local"* ]]; then
    printf '%s\n' "${endpoint}"
    return 0
  fi

  kubectl port-forward -n "${namespace_mlops}" svc/minio "${local_port}:9000" >"${log_path}" 2>&1 &
  kfp_port_forward_pids+=("$!")
  wait_for_local_port "${local_port}" "model store endpoint" || {
    cat "${log_path}" >&2 || true
    return 1
  }
  printf 'http://127.0.0.1:%s\n' "${local_port}"
}

configure_local_model_store_endpoint() {
  local endpoint="${MODEL_STORE_ENDPOINT:-${MLFLOW_S3_ENDPOINT_URL:-${MINIO_ENDPOINT:-}}}"
  endpoint="$(local_model_store_endpoint "${endpoint}")"
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
  helm upgrade --install recsys-data-platform infra/helm/recsys-data-platform \
    --namespace "${namespace_data}" \
    --create-namespace \
    --reuse-values \
    --timeout "${timeout}" \
    --wait \
    --wait-for-jobs \
    --set "images.pullPolicy=Always" \
    --set "spark.driverMemory=${SPARK_K8S_DRIVER_MEMORY:-2g}" \
    --set "spark.driverMemoryOverhead=${SPARK_K8S_DRIVER_MEMORY_OVERHEAD:-768m}" \
    --set "spark.executorMemory=${SPARK_K8S_EXECUTOR_MEMORY:-4g}" \
    --set "spark.executorMemoryOverhead=${SPARK_K8S_EXECUTOR_MEMORY_OVERHEAD:-1g}" \
    --set "spark.executorInstances=${SPARK_K8S_EXECUTOR_INSTANCES:-1}" \
    --set "spark.sqlShufflePartitions=${SPARK_SQL_SHUFFLE_PARTITIONS:-16}" \
    --set "flinkTaskManager.replicas=${FLINK_TASKMANAGER_REPLICAS:-2}" \
    --set "flinkTaskManager.resources.requests.cpu=${FLINK_TASKMANAGER_REQUEST_CPU:-500m}" \
    --set "flinkTaskManager.resources.requests.memory=${FLINK_TASKMANAGER_REQUEST_MEMORY:-4Gi}" \
    --set "flinkTaskManager.resources.limits.cpu=${FLINK_TASKMANAGER_LIMIT_CPU:-2}" \
    --set "flinkTaskManager.resources.limits.memory=${FLINK_TASKMANAGER_LIMIT_MEMORY:-8Gi}" \
    --set "flink.taskSlots=${FLINK_TASK_SLOTS:-1}" \
    --set "flink.taskManagerProcessMemory=${FLINK_TASKMANAGER_PROCESS_MEMORY:-6144m}" \
    --set "flink.taskManagerTaskHeapMemory=${FLINK_TASKMANAGER_TASK_HEAP_MEMORY:-3072m}" \
    --set "flink.taskManagerManagedMemory=${FLINK_TASKMANAGER_MANAGED_MEMORY:-512m}" \
    --set "flink.taskManagerJvmOverheadMax=${FLINK_TASKMANAGER_JVM_OVERHEAD_MAX:-2048m}" \
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
    --wait
    --set "kserve.enabled=false"
    --set "autoscaling.kserveResource.enabled=false"
    --set "api.namespace.name=${namespace_api}"
    --set "api.image=$(image recsys-api-serving)"
    --set "api.imagePullPolicy=Always"
    --set "featureApi.image=$(image recsys-api-serving)"
    --set "featureApi.imagePullPolicy=Always"
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
  RECSYS_MODEL_CD_ATOMIC="${RECSYS_MODEL_CD_ATOMIC:-0}" \
    uv run --no-project --with boto3 python jenkins/scripts/model_cd.py \
    --manifest-uri "${promotion_manifest_uri}" \
    --output-dir .model-cd \
    --apply \
    --timeout "${timeout}"
}

deploy_kserve() {
  with_file_lock "/tmp/recsys-serving-helm.lock" deploy_kserve_unlocked
}

deploy_kserve_model_cd() {
  with_file_lock "/tmp/recsys-serving-helm.lock" deploy_kserve_model_cd_unlocked
}

deploy_drift() {
  deploy_data_platform --set "images.dataflowCli=$(image recsys-dataflow-cli)"
  if [[ -d infra/knative/recsys-drift ]]; then
    kubectl apply -k infra/knative/recsys-drift
  else
    echo "No infra/knative/recsys-drift manifests yet; deployed drift-capable dataflow image only."
  fi
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
          --set "images.dataflowCli=$(image recsys-dataflow-cli)" \
          --set "images.airflow=$(image recsys-airflow)" \
          --set "images.kafkaConnect=$(image recsys-kafka-connect)"
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
  drift)
    deploy_drift
    ;;
  mlflow)
    deploy_mlflow
    ;;
  all)
    deploy_all
    ;;
  *)
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac
