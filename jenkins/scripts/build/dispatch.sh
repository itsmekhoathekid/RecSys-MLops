#!/usr/bin/env bash

build_dispatch() {
  case "$1" in
    materialize)
      build_feature_store
      ;;
    training)
      build_training
      build_mlops_spark
      build_mlflow
      build_compile_kfp_package
      build_drift_retrain
      ;;
    spark_batch)
      build_ensure_spark_base
      build_airflow
      ;;
    dp1)
      build_ensure_spark_base
      build_data_ingestion
      build_airflow
      build_kafka_connect
      ;;
    dp2)
      build_ensure_spark_base
      build_airflow
      ;;
    dp3)
      build_ensure_spark_base
      build_feature_store
      build_airflow
      ;;
    api)
      build_api
      ;;
    kserve)
      recsys_log "KServe uses Triton runtime plus model artifacts; no application image build is required."
      ;;
    rollout)
      build_training
      ;;
    drift)
      build_drift_retrain
      ;;
    stream_offline|stream_online)
      build_flink
      ;;
    analytics)
      build_analytics
      build_airflow
      ;;
    demo_web)
      build_demo
      ;;
    mlflow)
      build_mlflow
      ;;
    all)
      build_ensure_base_python
      build_data_ingestion
      build_feature_store
      build_training
      build_mlops_spark
      build_compile_kfp_package
      build_drift_retrain
      build_airflow
      build_kafka_connect
      build_mlflow
      build_flink
      build_api
      build_demo
      build_analytics
      recsys_log "built/published all RecSys service images for ${BUILD_IMAGE_TAG}"
      ;;
    *)
      recsys_error "unknown component: $1"
      return 2
      ;;
  esac
}
