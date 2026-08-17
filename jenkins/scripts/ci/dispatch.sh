#!/usr/bin/env bash

run_component_ci() {
  case "$1" in
    materialize) ci_materialize ;;
    training) ci_training ;;
    dp1) ci_dp1 ;;
    dp2) ci_dp2 ;;
    dp3) ci_dp3 ;;
    rag_index) ci_rag_index ;;
    rag_api) ci_rag_api ;;
    online_feature_api) ci_online_feature_api ;;
    inference_api) ci_inference_api ;;
    kserve) ci_kserve ;;
    rollout) ci_rollout ;;
    drift) ci_drift ;;
    stream_offline) ci_stream_offline ;;
    stream_online) ci_stream_online ;;
    analytics) ci_analytics ;;
    demo_web) ci_demo_web ;;
    *)
      echo "Unknown component: $1" >&2
      return 2
      ;;
  esac
}
