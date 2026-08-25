#!/usr/bin/env bash

component_verification_key() {
  case "$1" in
    stream_offline|stream_online) printf '%s\n' stream_features ;;
    *) printf '%s\n' "$1" ;;
  esac
}

run_component_verification() {
  case "$1" in
    materialize) test_materialize ;;
    training) test_training ;;
    mlflow) test_mlflow ;;
    dp2) test_dp2 ;;
    dp1) test_dp1 ;;
    dp3) test_dp3 ;;
    datahub_catalog) test_datahub_catalog ;;
    rag_index) test_rag_index ;;
    rag_api) test_rag_api ;;
    feature_rag_mcp) test_feature_rag_mcp ;;
    context_agent) test_context_agent ;;
    recommendation_mcp) test_recommendation_mcp ;;
    recommendation_agent) test_recommendation_agent ;;
    coordinator_agent) test_coordinator_agent ;;
    online_feature_api) test_online_feature_api ;;
    inference_api) test_inference_api ;;
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
      test_feature_rag_mcp
      test_context_agent
      test_recommendation_mcp
      test_recommendation_agent
      test_coordinator_agent
      test_online_feature_api
      test_inference_api
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
