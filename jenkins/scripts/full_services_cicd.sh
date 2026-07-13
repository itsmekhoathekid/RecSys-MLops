#!/usr/bin/env bash
set -euo pipefail

image_registry="${IMAGE_REGISTRY:-${IMAGE_PUSH_REGISTRY:-asia-southeast1-docker.pkg.dev/fsds-coursework/recsys}}"
image_registry="${image_registry%/}"
image_tag="${IMAGE_TAG:-${GIT_COMMIT:-}}"
components_csv="${FULL_CICD_COMPONENTS:-materialize,training,spark_batch,dp1,dp2,dp3,api,kserve,rollout,drift,stream_offline,stream_online,analytics}"
kube_context="${KUBE_CONTEXT:-gke_fsds-coursework_asia-southeast1-b_recsys-mlops-gke}"
run_component_ci="${RUN_COMPONENT_CI:-1}"
run_build="${RUN_COMPONENT_BUILD:-1}"
run_deploy="${RUN_COMPONENT_DEPLOY:-1}"
run_data_e2e="${RUN_DATA_PLATFORM_E2E:-1}"
run_ml_e2e="${RUN_ML_PLATFORM_E2E:-1}"
run_post_deploy_e2e="${RUN_POST_DEPLOY_E2E:-1}"
run_node_rebalance="${RUN_NODE_REBALANCE:-1}"
validate_node_rebalance="${VALIDATE_NODE_REBALANCE:-1}"
build_backend="${FULL_CICD_BUILD_BACKEND:-docker}"

export UV_CACHE_DIR="${UV_CACHE_DIR:-.uv-cache}"

if [[ -z "${image_tag}" ]]; then
  image_tag="$(git rev-parse --short=12 HEAD)"
fi

IFS=',' read -r -a components <<<"${components_csv}"

section() {
  printf '\n== %s ==\n' "$1"
}

prepare_kfp_package_for_contracts() {
  section "Prepare Kubeflow Package"
  KFP_UPLOAD_PACKAGE=0 \
    RECSYS_PIPELINE_IMAGE="${image_registry}/recsys-mlops-training:${image_tag}" \
    RECSYS_RAY_IMAGE="${image_registry}/recsys-mlops-training:${image_tag}" \
    RECSYS_SPARK_IMAGE="${image_registry}/recsys-mlops-spark:${image_tag}" \
    bash jenkins/scripts/kubeflow_pipeline_cicd.sh
}

run_component_ci_gates() {
  section "Component CI"
  for component in "${components[@]}"; do
    COVERAGE_MIN="${COVERAGE_MIN:-90}" \
      RECSYS_PIPELINE_IMAGE="${image_registry}/recsys-mlops-training:${image_tag}" \
      RECSYS_RAY_IMAGE="${image_registry}/recsys-mlops-training:${image_tag}" \
      RECSYS_SPARK_IMAGE="${image_registry}/recsys-mlops-spark:${image_tag}" \
      bash jenkins/scripts/component_ci.sh "${component}"
  done
  bash jenkins/scripts/helm_dry_run.sh
}

build_all_images() {
  section "Build And Publish All Images"
  case "${build_backend}" in
    cloudbuild)
      gcloud builds submit \
        --config infra/cloudbuild/recsys-images.yaml \
        --substitutions "_IMAGE_REPO=${image_registry},_TAG=${image_tag}" \
        .
      ;;
    docker)
      IMAGE_PUSH_REGISTRY="${image_registry}" IMAGE_TAG="${image_tag}" bash jenkins/scripts/component_build_publish.sh all
      ;;
    *)
      echo "Unknown FULL_CICD_BUILD_BACKEND=${build_backend}; expected docker or cloudbuild." >&2
      return 2
      ;;
  esac
}

deploy_all_services() {
  section "Deploy All Services"
  IMAGE_PULL_REGISTRY="${IMAGE_PULL_REGISTRY:-${image_registry}}" \
    IMAGE_TAG="${image_tag}" \
    RUN_NODE_REBALANCE="${run_node_rebalance}" \
    VALIDATE_NODE_REBALANCE="${validate_node_rebalance}" \
    bash jenkins/scripts/component_deploy.sh all
}

run_data_platform_e2e() {
  section "Data Platform E2E"
  KUBE_CONTEXT="${kube_context}" \
  RECSYS_DATA_SETUP_SKIP_CLUSTER_UP="${RECSYS_DATA_SETUP_SKIP_CLUSTER_UP:-1}" \
    RECSYS_DATA_SETUP_SKIP_INSTALL="${RECSYS_DATA_SETUP_SKIP_INSTALL:-1}" \
    infra/k8s/scripts/cluster_data_setup.sh
}

run_ml_platform_e2e() {
  section "ML Platform E2E"
  KUBE_CONTEXT="${kube_context}" \
  RECSYS_E2E_SKIP_CLUSTER_UP="${RECSYS_E2E_SKIP_CLUSTER_UP:-1}" \
    RECSYS_E2E_RUN_DATA_SETUP="${RECSYS_E2E_RUN_DATA_SETUP:-0}" \
    RECSYS_PIPELINE_IMAGE="${RECSYS_PIPELINE_IMAGE:-${image_registry}/recsys-mlops-training:${image_tag}}" \
    RECSYS_RAY_IMAGE="${RECSYS_RAY_IMAGE:-${image_registry}/recsys-mlops-training:${image_tag}}" \
    RECSYS_SPARK_IMAGE="${RECSYS_SPARK_IMAGE:-${image_registry}/recsys-mlops-spark:${image_tag}}" \
    infra/k8s/scripts/cluster_mlops_serving_e2e.sh
}

run_post_deploy_validation() {
  section "Post Deploy E2E"
  jenkins/scripts/post_deploy_e2e.sh
}

echo "Full RecSys CI/CD tag: ${image_tag}"
echo "Image registry: ${image_registry}"
echo "Build backend: ${build_backend}"
echo "Node rebalance after deploy: ${run_node_rebalance}; validate: ${validate_node_rebalance}"

if [[ "${run_component_ci}" == "1" || "${run_component_ci}" == "true" ]]; then
  prepare_kfp_package_for_contracts
  run_component_ci_gates
fi
if [[ "${run_build}" == "1" || "${run_build}" == "true" ]]; then
  build_all_images
fi
if [[ "${run_deploy}" == "1" || "${run_deploy}" == "true" ]]; then
  deploy_all_services
fi
if [[ "${run_data_e2e}" == "1" || "${run_data_e2e}" == "true" ]]; then
  run_data_platform_e2e
fi
if [[ "${run_ml_e2e}" == "1" || "${run_ml_e2e}" == "true" ]]; then
  run_ml_platform_e2e
fi
if [[ "${run_post_deploy_e2e}" == "1" || "${run_post_deploy_e2e}" == "true" ]]; then
  run_post_deploy_validation
fi

section "Full CI/CD Complete"
echo "All requested CI/CD and E2E stages completed for ${image_registry}:${image_tag}."
