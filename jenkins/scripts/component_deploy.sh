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

deploy_data_platform() {
  helm upgrade --install recsys-data-platform infra/helm/recsys-data-platform \
    --namespace "${namespace_data}" \
    --create-namespace \
    --reuse-values \
    --timeout "${timeout}" \
    --set "images.pullPolicy=Always" \
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
    --set "api.namespace.name=${namespace_api}" \
    --set "api.image=$(image recsys-api-serving)" \
    --set "api.imagePullPolicy=Always" \
    --set "featureApi.image=$(image recsys-api-serving)" \
    --set "featureApi.imagePullPolicy=Always" \
    "${rollout_args[@]}"
  kubectl rollout status "deployment/recsys-online-feature-api" -n "${namespace_api}" --timeout="${timeout}"
  kubectl rollout status "deployment/recsys-api-serving" -n "${namespace_api}" --timeout="${timeout}"
}

deploy_training_refs() {
  PYTHONPATH=apps/ml-system/src:apps/data-platform/src \
    RECSYS_PIPELINE_IMAGE="$(image recsys-mlops-training)" \
    RECSYS_SPARK_IMAGE="$(image recsys-mlops-spark)" \
    uv run python apps/ml-system/src/kubeflow/pipelines/compile_training_pipeline.py

  helm upgrade --install recsys-ray-cpu infra/helm/ray-cluster \
    --namespace "${namespace_kubeflow}" \
    --create-namespace \
    --timeout "${timeout}" \
    --take-ownership \
    --force-conflicts \
    --set "image.repository=${image_registry}/recsys-mlops-training" \
    --set "image.tag=${image_tag}" \
    --set "image.pullPolicy=Always"
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
        ;;
      spark_batch|dp2)
        deploy_data_platform --set "images.spark=$(image recsys-spark)" --set "images.airflow=$(image recsys-airflow)"
        ;;
      dp1)
        deploy_data_platform \
          --set "images.dataflowCli=$(image recsys-dataflow-cli)" \
          --set "images.airflow=$(image recsys-airflow)" \
          --set "images.kafkaConnect=$(image recsys-kafka-connect)"
        ;;
      dp3)
        deploy_data_platform \
          --set "images.spark=$(image recsys-spark)" \
          --set "images.dataflowCli=$(image recsys-dataflow-cli)" \
          --set "images.airflow=$(image recsys-airflow)"
        ;;
      stream_offline)
        deploy_data_platform --set "images.flink=$(image recsys-flink)"
        ;;
      stream_online)
        deploy_data_platform --set "images.flink=$(image recsys-flink)" --set "images.dataflowCli=$(image recsys-dataflow-cli)"
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
