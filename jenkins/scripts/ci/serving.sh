#!/usr/bin/env bash

ci_online_feature_api() {
  tests=(
    tests/unit/api_serving/test_serving.py
    tests/unit/api_serving/test_split_services.py
    tests/unit/api_serving/test_validation_verification.py
    tests/contract/test_serving_contracts.py
  )
  append_integration_dir online_feature_api
  cov_paths=(recsys_online_feature_api recsys_serving_common)
  run_configured_component_tests "${component}" "apps/api-serving/online-feature-api/src:apps/api-serving/inference-api/src:apps/api-serving/shared/src:apps/data-platform/feature-store/runtime/src"
}

ci_inference_api() {
  tests=(
    tests/unit/api_serving/test_serving.py
    tests/unit/api_serving/test_split_services.py
    tests/unit/api_serving/test_validation_verification.py
    tests/contract/test_serving_contracts.py
    tests/contract/test_gateway_contracts.py
  )
  append_integration_dir inference_api
  cov_paths=(recsys_inference_api recsys_serving_common)
  run_configured_component_tests "${component}" "apps/api-serving/inference-api/src:apps/api-serving/online-feature-api/src:apps/api-serving/shared/src:apps/data-platform/feature-store/runtime/src"
}

ci_rag_api() {
  tests=(tests/unit/api_serving/rag_api)
  cov_paths=(recsys_rag_api)
  run_configured_component_tests "${component}" "apps/api-serving/rag-api/src:apps/api-serving/shared/src:apps/data-platform/rag-runtime/src"
  "${ci_environment}/bin/interrogate" \
    --fail-under 90 \
    --ignore-init-method \
    --ignore-private \
    --ignore-semiprivate \
    --ignore-property-decorators \
    apps/api-serving/rag-api/src/recsys_rag_api
  helm lint infra/helm/recsys-rag-api -f infra/helm/recsys-rag-api/values-gcp.yaml
  helm template recsys-rag-api infra/helm/recsys-rag-api \
    -f infra/helm/recsys-rag-api/values-gcp.yaml \
    --set-string image=registry.example.invalid/recsys/recsys-rag-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
    >/dev/null
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
