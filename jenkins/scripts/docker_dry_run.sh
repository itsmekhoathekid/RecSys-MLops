#!/usr/bin/env bash
set -euo pipefail

component="${1:-all}"

build_base() {
  docker build -f infra/docker/Dockerfile.base-python -t recsys-base-python:ci .
}

case "${component}" in
  base)
    build_base
    ;;
  data-ingestion)
    build_base
    docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/Dockerfile.data-ingestion -t recsys-data-ingestion:ci .
    ;;
  data-runtimes)
    docker build -f apps/data-platform/Dockerfile.spark -t recsys-spark:ci .
    docker build -f apps/data-platform/Dockerfile.flink -t recsys-flink:ci .
    ;;
  feature-store)
    build_base
    docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/Dockerfile.feature-store -t recsys-feature-store:ci .
    ;;
  drift-retrain)
    build_base
    docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/Dockerfile.drift-retrain -t recsys-drift-retrain:ci .
    ;;
  training)
    build_base
    docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/ml-system/Dockerfile.training -t recsys-mlops-training:ci .
    ;;
  all)
    "$0" data-ingestion
    "$0" feature-store
    "$0" drift-retrain
    "$0" data-runtimes
    "$0" training
    ;;
  *)
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac
