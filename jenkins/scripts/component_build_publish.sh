#!/usr/bin/env bash
set -euo pipefail

component="${1:?component is required}"
image_registry="${IMAGE_PUSH_REGISTRY:-${IMAGE_REGISTRY:-localhost:5001/recsys}}"
image_registry="${image_registry%/}"
registry_host="${image_registry%%/*}"
image_tag="${IMAGE_TAG:-${GIT_COMMIT:-}}"
publish_images="${PUBLISH_IMAGES:-1}"
require_gcp_artifact_registry="${REQUIRE_GCP_ARTIFACT_REGISTRY:-1}"
manifest_dir="${IMAGE_MANIFEST_DIR:-.ci-image-manifest}"
docker_platform="${DOCKER_PLATFORM:-linux/amd64}"

if [[ -z "${image_tag}" ]]; then
  image_tag="$(git rev-parse --short=12 HEAD)"
fi

if [[ "${require_gcp_artifact_registry}" == "1" || "${require_gcp_artifact_registry}" == "true" ]]; then
  if [[ "${image_registry}" != *".pkg.dev/"* ]]; then
    echo "REQUIRE_GCP_ARTIFACT_REGISTRY is enabled, but IMAGE_PUSH_REGISTRY is not a GCP Artifact Registry repo: ${image_registry}" >&2
    exit 2
  fi
  if [[ "${publish_images}" != "1" && "${publish_images}" != "true" ]]; then
    echo "REQUIRE_GCP_ARTIFACT_REGISTRY is enabled, so PUBLISH_IMAGES must be true." >&2
    exit 2
  fi
fi

mkdir -p "${manifest_dir}"
manifest_path="${manifest_dir}/${component}.env"
: >"${manifest_path}"

build_base_python=0
build_spark_base=0

record_image() {
  local key="$1"
  local image="$2"
  printf '%s=%s\n' "${key}" "${image}" >>"${manifest_path}"
}

refresh_registry_login_if_gcp() {
  if [[ "${publish_images}" != "1" && "${publish_images}" != "true" ]]; then
    return 0
  fi
  if [[ "${registry_host}" != *".pkg.dev" ]]; then
    return 0
  fi

  local token=""
  if token="$(
    curl -fsS -H 'Metadata-Flavor: Google' \
      'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])' 2>/dev/null
  )"; then
    :
  elif command -v gcloud >/dev/null 2>&1; then
    token="$(gcloud auth print-access-token)"
  else
    echo "Unable to obtain an Artifact Registry access token from metadata server or gcloud." >&2
    return 1
  fi
  echo "${token}" | docker login "https://${registry_host}" --username oauth2accesstoken --password-stdin >/dev/null
  echo "Refreshed Docker login for ${registry_host}"
}

build_and_optionally_push() {
  local name="$1"
  local dockerfile="$2"
  shift 2
  local local_image="${name}:${image_tag}"
  local remote_image="${image_registry}/${name}:${image_tag}"

  docker build --platform "${docker_platform}" "$@" -f "${dockerfile}" -t "${local_image}" .
  docker tag "${local_image}" "${remote_image}"
  record_image "$(echo "${name}" | tr '[:lower:]-' '[:upper:]_')_IMAGE" "${remote_image}"
  if [[ "${publish_images}" == "1" || "${publish_images}" == "true" ]]; then
    refresh_registry_login_if_gcp
    docker push "${remote_image}"
  else
    echo "Skipping docker push for ${remote_image}; PUBLISH_IMAGES=${publish_images}"
  fi
}

ensure_base_python() {
  if [[ "${build_base_python}" == "0" ]]; then
    docker build --platform "${docker_platform}" -f infra/docker/Dockerfile.base-python -t "recsys-base-python:${image_tag}" .
    build_base_python=1
  fi
}

ensure_spark_base() {
  if [[ "${build_spark_base}" == "0" ]]; then
    build_and_optionally_push "recsys-spark" "apps/data-platform/Dockerfile.spark"
    build_spark_base=1
  fi
}

build_dataflow_cli() {
  ensure_base_python
  build_and_optionally_push "recsys-dataflow-cli" "apps/data-platform/Dockerfile.dataflow-cli" \
    --build-arg "RECSYS_BASE_IMAGE=recsys-base-python:${image_tag}"
}

build_data_generator() {
  ensure_base_python
  build_and_optionally_push "recsys-data-generator" "apps/data-platform/data-generator/Dockerfile" \
    --build-arg "RECSYS_BASE_IMAGE=recsys-base-python:${image_tag}"
}

build_airflow() {
  build_and_optionally_push "recsys-airflow" "infra/docker/Dockerfile.airflow"
}

build_kafka_connect() {
  build_and_optionally_push "recsys-kafka-connect" "infra/docker/Dockerfile.kafka-connect"
}

build_mlflow() {
  build_and_optionally_push "recsys-mlflow" "infra/docker/Dockerfile.mlflow"
}

build_training() {
  ensure_base_python
  build_and_optionally_push "recsys-mlops-training" "apps/ml-system/Dockerfile.training" \
    --build-arg "RECSYS_BASE_IMAGE=recsys-base-python:${image_tag}"
}

build_mlops_spark() {
  ensure_spark_base
  build_and_optionally_push "recsys-mlops-spark" "apps/ml-system/Dockerfile.spark" \
    --build-arg "RECSYS_SPARK_BASE_IMAGE=recsys-spark:${image_tag}"
}

build_flink() {
  build_and_optionally_push "recsys-flink" "apps/data-platform/Dockerfile.flink"
}

build_api() {
  build_and_optionally_push "recsys-api-serving" "apps/api-serving/Dockerfile"
}

build_analytics() {
  ensure_spark_base
  build_and_optionally_push "recsys-analytics-spark" "apps/analytics/Dockerfile.spark" \
    --build-arg "RECSYS_SPARK_BASE_IMAGE=recsys-spark:${image_tag}"
  build_and_optionally_push "recsys-analytics-dbt" "apps/analytics/Dockerfile.dbt"
  build_and_optionally_push "recsys-analytics-superset" "apps/analytics/Dockerfile.superset"
  build_airflow
}

compile_kfp_package_for_image_refs() {
  local training_image="${image_registry}/recsys-mlops-training:${image_tag}"
  local spark_image="${image_registry}/recsys-mlops-spark:${image_tag}"

  KFP_UPLOAD_PACKAGE=0 \
    RECSYS_PIPELINE_IMAGE="${training_image}" \
    RECSYS_RAY_IMAGE="${training_image}" \
    RECSYS_SPARK_IMAGE="${spark_image}" \
    bash jenkins/scripts/kubeflow_pipeline_cicd.sh
}

case "${component}" in
  materialize)
    build_dataflow_cli
    ;;
  training)
    build_training
    build_mlops_spark
    compile_kfp_package_for_image_refs
    build_dataflow_cli
    ;;
  spark_batch)
    ensure_spark_base
    build_airflow
    ;;
  dp1)
    build_data_generator
    build_dataflow_cli
    build_airflow
    build_kafka_connect
    ;;
  dp2)
    ensure_spark_base
    build_airflow
    ;;
  dp3)
    ensure_spark_base
    build_dataflow_cli
    build_airflow
    ;;
  api)
    build_api
    ;;
  kserve)
    echo "KServe uses Triton runtime plus model artifacts; no application image build is required." | tee -a "${manifest_path}"
    ;;
  drift)
    build_dataflow_cli
    ;;
  stream_offline)
    build_flink
    ;;
  stream_online)
    build_flink
    build_dataflow_cli
    ;;
  analytics)
    build_analytics
    ;;
  mlflow)
    build_mlflow
    ;;
  all)
    build_training
    build_mlops_spark
    compile_kfp_package_for_image_refs
    build_dataflow_cli
    build_data_generator
    build_airflow
    build_kafka_connect
    build_mlflow
    build_flink
    build_api
    build_analytics
    echo "Built/published all RecSys service images and compiled the Kubeflow package for ${image_registry}:${image_tag}."
    ;;
  *)
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac

echo "Wrote image manifest: ${manifest_path}"
