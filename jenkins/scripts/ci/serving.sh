#!/usr/bin/env bash

ci_api() {
  tests=(tests/unit/api_serving tests/contract/test_serving_contracts.py tests/contract/test_gateway_contracts.py)
  append_integration_dir api
  cov_paths=(ab_testing api_runtime api_schemas feature_api feature_service_client inference_api online_features ranking serving_utils shadow triton)
  run_configured_component_tests "${component}" "apps/api-serving/src"
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
