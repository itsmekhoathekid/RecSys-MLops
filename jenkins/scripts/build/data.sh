#!/usr/bin/env bash

build_data_ingestion() {
  build_ensure_base_python
  build_image "recsys-data-ingestion" "apps/data-platform/Dockerfile.data-ingestion" \
    --build-arg "RECSYS_BASE_IMAGE=recsys-base-python:${BUILD_IMAGE_TAG}"
}

build_feature_store() {
  build_ensure_base_python
  build_image "recsys-feature-store" "apps/data-platform/Dockerfile.feature-store" \
    --build-arg "RECSYS_BASE_IMAGE=recsys-base-python:${BUILD_IMAGE_TAG}"
}

build_drift_retrain() {
  build_ensure_base_python
  build_image "recsys-drift-retrain" "apps/data-platform/Dockerfile.drift-retrain" \
    --build-arg "RECSYS_BASE_IMAGE=recsys-base-python:${BUILD_IMAGE_TAG}"
}

build_airflow() {
  build_image "recsys-airflow" "infra/docker/Dockerfile.airflow"
}

build_kafka_connect() {
  build_image "recsys-kafka-connect" "infra/docker/Dockerfile.kafka-connect"
}

build_flink() {
  build_image "recsys-flink" "apps/data-platform/Dockerfile.flink"
}
