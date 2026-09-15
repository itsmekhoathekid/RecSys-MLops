#!/usr/bin/env bash

mcp_auth_manifest_gate() {
  local manifest="configs/agentic/mcp-auth-versions.yaml"
  local base_ref="${CI_BASE_REF:-HEAD^}"
  local previous command_status=0
  python3 ops/security/mcp_auth_versions.py validate "${manifest}"
  if git rev-parse --verify "${base_ref}" >/dev/null 2>&1 \
    && git cat-file -e "${base_ref}:${manifest}" >/dev/null 2>&1 \
    && ! git diff --quiet "${base_ref}" HEAD -- "${manifest}"; then
    previous="$(mktemp)"
    git show "${base_ref}:${manifest}" >"${previous}"
    python3 ops/security/mcp_auth_versions.py validate-transition \
      "${previous}" "${manifest}" || command_status=$?
    rm -f -- "${previous}"
    return "${command_status}"
  fi
}

agentic_static_checks() {
  local source_root="apps/agentic/recsys-feature-rag-mcp/src"
  PYTHONPATH="${source_root}" "${ci_environment}/bin/ruff" check \
    "${source_root}" tests/unit/agentic tests/contract/test_agentic_context_contracts.py
  PYTHONPATH="${source_root}" "${ci_environment}/bin/mypy" \
    "${source_root}/recsys_feature_rag_mcp"
  "${ci_python}" -m compileall -q "${source_root}"
  "${ci_environment}/bin/interrogate" \
    --fail-under 90 \
    --ignore-init-method \
    --ignore-private \
    --ignore-semiprivate \
    --ignore-property-decorators \
    "${source_root}/recsys_feature_rag_mcp"
}

agentic_helm_gate() {
  local chart="$1"
  local rendered
  local values_args=()
  rendered="${reports_dir}/$(basename "${chart}")-rendered.yaml"
  [[ -f "${chart}/values-gcp.yaml" ]] && values_args=(-f "${chart}/values-gcp.yaml")
  case "$(basename "${chart}")" in
    recsys-security)
      mcp_auth_manifest_gate
      values_args+=(-f configs/agentic/mcp-auth-versions.yaml)
      values_args+=(
        --set externalSecrets.featureRagMcp.enabled=true
        --set externalSecrets.recommendationMcp.enabled=true
        --set istio.enabled=true
      )
      ;;
    recsys-feature-rag-mcp|recsys-kagent-agent|recsys-recommendation-mcp|recsys-recommendation-agent)
      mcp_auth_manifest_gate
      values_args+=(-f configs/agentic/mcp-auth-versions.yaml)
      ;;
  esac
  helm lint "${chart}" "${values_args[@]}"
  helm template contract-test "${chart}" "${values_args[@]}" >"${rendered}"
  command -v kubeconform >/dev/null 2>&1 || {
    echo "kubeconform is required for agentic Helm CI" >&2
    return 2
  }
  kubeconform -strict -summary -ignore-missing-schemas "${rendered}"
}

ci_feature_rag_mcp() {
  tests=(
    tests/unit/agentic/feature_rag_mcp
    tests/unit/jenkins/test_mcp_auth_rotation_pipeline.py
    tests/contract/test_agentic_context_contracts.py
    tests/contract/test_mcp_auth_rotation_contracts.py
  )
  append_integration_dir feature_rag_mcp
  cov_paths=(recsys_feature_rag_mcp)
  run_configured_component_tests \
    "${component}" \
    "apps/agentic/recsys-feature-rag-mcp/src"
  agentic_static_checks
  agentic_helm_gate infra/helm/recsys-security
  agentic_helm_gate infra/helm/recsys-feature-rag-mcp
  agentic_helm_gate infra/helm/recsys-kagent-agent
}

ci_context_agent() {
  agentic_helm_gate infra/helm/recsys-global-model-config
  run_plain_pytest_with_pythonpath_override \
    "${component}" \
    "apps/agentic/recsys-feature-rag-mcp/src" \
    tests/unit/jenkins/test_global_model_config.py \
    tests/unit/jenkins/test_mcp_auth_rotation_pipeline.py \
    tests/contract/test_agentic_context_contracts.py \
    tests/contract/test_mcp_auth_rotation_contracts.py \
    tests/e2e/agentic_context
  agentic_helm_gate infra/helm/recsys-kagent-agent
}

recommendation_agentic_static_checks() {
  local source_root="apps/agentic/recsys-recommendation-mcp/src"
  PYTHONPATH="${source_root}" "${ci_environment}/bin/ruff" check \
    "${source_root}" tests/unit/agentic/recommendation_mcp \
    tests/contract/test_recommendation_agentic_contracts.py
  PYTHONPATH="${source_root}" "${ci_environment}/bin/mypy" \
    "${source_root}/recsys_recommendation_mcp"
  "${ci_python}" -m compileall -q "${source_root}"
  "${ci_environment}/bin/interrogate" \
    --fail-under 90 \
    --ignore-init-method \
    --ignore-private \
    --ignore-semiprivate \
    --ignore-property-decorators \
    "${source_root}/recsys_recommendation_mcp"
}

ci_recommendation_mcp() {
  tests=(
    tests/unit/agentic/recommendation_mcp
    tests/unit/jenkins/test_mcp_auth_rotation_pipeline.py
    tests/contract/test_recommendation_agentic_contracts.py
    tests/contract/test_mcp_auth_rotation_contracts.py
  )
  append_integration_dir recommendation_agentic
  cov_paths=(recsys_recommendation_mcp)
  run_configured_component_tests \
    "${component}" \
    "apps/agentic/recsys-recommendation-mcp/src"
  recommendation_agentic_static_checks
  bash jenkins/scripts/ci/recommendation_mutation.sh "${ci_environment}"
  agentic_helm_gate infra/helm/recsys-security
  agentic_helm_gate infra/helm/recsys-recommendation-mcp
  agentic_helm_gate infra/helm/recsys-recommendation-agent
}

ci_recommendation_agent() {
  agentic_helm_gate infra/helm/recsys-global-model-config
  run_plain_pytest_with_pythonpath_override \
    "${component}" \
    "apps/agentic/recsys-recommendation-mcp/src" \
    tests/unit/jenkins/test_global_model_config.py \
    tests/unit/jenkins/test_mcp_auth_rotation_pipeline.py \
    tests/contract/test_recommendation_agentic_contracts.py \
    tests/contract/test_mcp_auth_rotation_contracts.py \
    tests/e2e/recommendation_agentic
  agentic_helm_gate infra/helm/recsys-recommendation-agent
}

ci_coordinator_agent() {
  agentic_helm_gate infra/helm/recsys-global-model-config
  run_plain_pytest_with_pythonpath_override \
    "${component}" \
    "apps/agentic/recsys-feature-rag-mcp/src" \
    tests/unit/jenkins/test_global_model_config.py \
    tests/contract/test_coordinator_agentic_contracts.py \
    tests/contract/test_mcp_auth_rotation_contracts.py \
    tests/e2e/coordinator_agentic
  agentic_helm_gate infra/helm/recsys-coordinator-agent
}
