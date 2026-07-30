#!/usr/bin/env bash

component_test_dispatch() {
  case "$1" in
    materialize) test_materialize ;;
    training) test_training ;;
    mlflow) test_mlflow ;;
    dp2) test_dp2 ;;
    dp1) test_dp1 ;;
    dp3) test_dp3 ;;
    api) test_api ;;
    kserve|kserve_model_cd) test_kserve ;;
    rollout) test_rollout ;;
    drift) test_drift ;;
    stream_offline|stream_online) test_stream_features ;;
    analytics) test_analytics ;;
    demo_web) test_demo ;;
    all)
      test_materialize
      test_dp1
      test_dp2
      test_dp3
      test_stream_features
      test_training
      test_kserve
      test_rollout
      test_analytics
      test_demo
      ;;
    *)
      recsys_error "no production test profile for component: $1"
      return 2
      ;;
  esac
}
