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
  data-generator)
    build_base
    docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/data-generator/Dockerfile -t recsys-data-generator:ci .
    ;;
  dataflow)
    docker build -f apps/data-platform/Dockerfile.spark -t recsys-spark:ci .
    docker build -f apps/data-platform/Dockerfile.flink -t recsys-flink:ci .
    build_base
    docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/Dockerfile.dataflow-cli -t recsys-dataflow-cli:ci .
    ;;
  feature-store)
    build_base
    docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/data-platform/feature-store/Dockerfile -t recsys-feature-store:ci .
    ;;
  training)
    build_base
    docker build --build-arg RECSYS_BASE_IMAGE=recsys-base-python:ci -f apps/ml-system/Dockerfile.training -t recsys-mlops-training:ci .
    ;;
  all)
    "$0" data-generator
    "$0" dataflow
    "$0" feature-store
    "$0" training
    ;;
  *)
    echo "Unknown component: ${component}" >&2
    exit 2
    ;;
esac

