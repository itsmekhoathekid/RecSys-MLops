#!/usr/bin/env bash

build_mlflow() {
  build_image "recsys-mlflow" "infra/docker/Dockerfile.mlflow"
}

build_training() {
  build_ensure_base_python
  build_image "recsys-mlops-training" "apps/ml-system/Dockerfile.training" \
    --build-arg "RECSYS_BASE_IMAGE=recsys-base-python:${BUILD_IMAGE_TAG}"
}

build_mlops_spark() {
  build_ensure_spark_base
  build_image "recsys-mlops-spark" "apps/ml-system/Dockerfile.spark" \
    --build-arg "RECSYS_SPARK_BASE_IMAGE=recsys-spark:${BUILD_IMAGE_TAG}"
}

build_compile_kfp_package() {
  KFP_UPLOAD_PACKAGE=0 \
    RECSYS_PIPELINE_IMAGE="${BUILD_IMAGE_REGISTRY}/recsys-mlops-training:${BUILD_IMAGE_TAG}" \
    RECSYS_RAY_IMAGE="${BUILD_IMAGE_REGISTRY}/recsys-mlops-training:${BUILD_IMAGE_TAG}" \
    RECSYS_SPARK_IMAGE="${BUILD_IMAGE_REGISTRY}/recsys-mlops-spark:${BUILD_IMAGE_TAG}" \
    bash jenkins/scripts/kubeflow_pipeline_cicd.sh
}
