#!/usr/bin/env bash

ci_online_feature_api() {
  tests=(tests/unit/api_serving tests/contract/test_serving_contracts.py)
  append_integration_dir online_feature_api
  cov_paths=(recsys_online_feature_api recsys_serving_common)
  run_configured_component_tests "${component}" "apps/api-serving/online-feature-api/src:apps/api-serving/inference-api/src:apps/api-serving/shared/src:packages/recsys-feature-store-runtime/src"
}

ci_inference_api() {
  tests=(tests/unit/api_serving tests/contract/test_serving_contracts.py tests/contract/test_gateway_contracts.py)
  append_integration_dir inference_api
  cov_paths=(recsys_inference_api recsys_serving_common)
  run_configured_component_tests "${component}" "apps/api-serving/inference-api/src:apps/api-serving/online-feature-api/src:apps/api-serving/shared/src:packages/recsys-feature-store-runtime/src"
}

ci_kserve() {
  tests=(tests/unit/ml_system/test_model_promotion.py tests/contract/test_serving_contracts.py)
  append_integration_dir kserve
  cov_paths=(
    jenkins.python.model_cd.cli
    jenkins.python.model_cd.config
    jenkins.python.model_cd.helm_release
    jenkins.python.model_cd.manifests
    jenkins.python.model_cd.promotion_gates
  )
  run_configured_component_tests "${component}" ".:apps/ml-system/src:apps/data-platform/src"
}
