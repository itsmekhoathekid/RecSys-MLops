#!/usr/bin/env bash

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
    tests/contract/test_agentic_context_contracts.py
  )
  append_integration_dir feature_rag_mcp
  cov_paths=(recsys_feature_rag_mcp)
  run_configured_component_tests \
    "${component}" \
    "apps/agentic/recsys-feature-rag-mcp/src"
  agentic_static_checks
  agentic_helm_gate infra/helm/recsys-feature-rag-mcp
  agentic_helm_gate infra/helm/recsys-kagent-agent
}

ci_context_agent() {
  run_plain_pytest_with_pythonpath_override \
    "${component}" \
    "apps/agentic/recsys-feature-rag-mcp/src" \
    tests/contract/test_agentic_context_contracts.py \
    tests/e2e/agentic_context
  agentic_helm_gate infra/helm/recsys-kagent-agent
}
