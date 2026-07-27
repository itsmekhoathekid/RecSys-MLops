#!/usr/bin/env bash

build_analytics() {
  build_ensure_spark_base
  build_image "recsys-analytics-spark" "apps/analytics/Dockerfile.spark" \
    --build-arg "RECSYS_SPARK_BASE_IMAGE=recsys-spark:${BUILD_IMAGE_TAG}"
  build_image "recsys-analytics-dbt" "apps/analytics/Dockerfile.dbt"
  build_image "recsys-analytics-superset" "apps/analytics/Dockerfile.superset"
}
