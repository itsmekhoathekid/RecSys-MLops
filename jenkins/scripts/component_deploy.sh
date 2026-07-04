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
promotion_manifest_uri="${PROMOTION_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/production.json}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"

if [[ -z "${image_tag}" ]]; then
  image_tag="$(git rev-parse --short=12 HEAD)"
fi

image() {
  printf '%s/%s:%s' "${image_registry}" "$1" "${image_tag}"
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

deploy_data_platform() {
  helm upgrade --install recsys-data-platform infra/helm/recsys-data-platform \
    --namespace "${namespace_data}" \
    --create-namespace \
    --reuse-values \
    --timeout "${timeout}" \
    --wait \
    --wait-for-jobs \
    --set "images.pullPolicy=Always" \
    --set "sourcePostgres.istioInject=false" \
    --set "airflowPostgres.istioInject=false" \
    --set "featurePostgres.istioInject=false" \
    --set "kafkaConnect.istioInject=false" \
    --set "redis.istioInject=false" \
    --set "flink.istioInject=false" \
    --set "realtimeFlinkConsumer.istioInject=false" \
    "$@"
}

deploy_api() {
  local rollout_args=()
  if [[ -n "${API_ROLLOUT_MAX_SURGE:-}" ]]; then
    rollout_args+=(--set "api.rollout.maxSurge=${API_ROLLOUT_MAX_SURGE}")
  fi
  if [[ -n "${API_ROLLOUT_MAX_UNAVAILABLE:-}" ]]; then
    rollout_args+=(--set "api.rollout.maxUnavailable=${API_ROLLOUT_MAX_UNAVAILABLE}")
  fi

  helm upgrade --install recsys-serving infra/helm/recsys-serving \
    --namespace "${namespace_kserve}" \
    --create-namespace \
    --reuse-values \
    --timeout "${timeout}" \
    --wait \
    --set "api.namespace.name=${namespace_api}" \
    --set "api.image=$(image recsys-api-serving)" \
    --set "api.imagePullPolicy=Always" \
    --set "featureApi.image=$(image recsys-api-serving)" \
    --set "featureApi.imagePullPolicy=Always" \
    "${rollout_args[@]}"
  verify_and_wait_workload "deployment" "recsys-online-feature-api" "${namespace_api}" "$(image recsys-api-serving)"
  verify_and_wait_workload "deployment" "recsys-api-serving" "${namespace_api}" "$(image recsys-api-serving)"
}

deploy_training_refs() {
  local compiled_package="infra/kubeflow/compiled/bst_training_pipeline.yaml"
  local training_image
  local spark_image

  training_image="$(image recsys-mlops-training)"
  spark_image="$(image recsys-mlops-spark)"

  PYTHONPATH=apps/ml-system/src:apps/data-platform/src \
    RECSYS_PIPELINE_IMAGE="${training_image}" \
    RECSYS_RAY_IMAGE="${training_image}" \
    RECSYS_SPARK_IMAGE="${spark_image}" \
    uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py

  grep -F "${training_image}" "${compiled_package}"
  grep -F "${spark_image}" "${compiled_package}"

  uv run python apps/ml-system/src/kubeflow/upload_pipeline_package.py \
    --host "${KFP_ENDPOINT:-http://ml-pipeline.kubeflow.svc.cluster.local:8888}" \
    --package-path "${compiled_package}" \
    --pipeline-name "${KFP_PIPELINE_NAME:-recsys-bst-feature-train-evaluate}"

  echo "Training CI/CD updated image refs and uploaded the Kubeflow pipeline package only; Ray Tune and DDP training are not auto-run by CI/CD."
}

deploy_kserve() {
  uv run python jenkins/scripts/model_cd.py \
    --manifest-uri "${promotion_manifest_uri}" \
    --output-dir .model-cd \
    --apply \
    --timeout "${timeout}"
}

deploy_drift() {
  deploy_data_platform --set "images.dataflowCli=$(image recsys-dataflow-cli)"
  if [[ -d infra/knative/recsys-drift ]]; then
    kubectl apply -k infra/knative/recsys-drift
  else
    echo "No infra/knative/recsys-drift manifests yet; deployed drift-capable dataflow image only."
  fi
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
  drift)
    deploy_drift
    ;;
  *)
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac
