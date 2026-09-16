#!/usr/bin/env bash

verify_agent_registry_runtime_lock() {
  local plan_file="$1"
  local lock_file="${2:-.ci-deploy/agent-registry-lock.json}"
  local evidence_file=".ci-deploy/agent-registry-runtime-verification.tsv"
  local context_file=".ci-deploy/agent-registry-runtime-context.tsv"
  local record_type artifact_id artifact_kind _workload_unit namespace
  local resource_name remote_key registry_ref release_version contract_sha image
  local resource_kind actual_annotations expected_annotations actual_image
  local record_count=0

  mkdir -p .ci-deploy
  if ! python3 -m jenkins.python.agent_registry_release runtime-context \
    --plan "${plan_file}" --lock "${lock_file}" >"${context_file}"; then
    return 2
  fi

  printf 'artifact\tkind\tnamespace\tresource\tregistryRef\treleaseVersion\tcontractSha256\timage\n' \
    >"${evidence_file}"
  while IFS=$'\t' read -r \
    record_type artifact_id artifact_kind _workload_unit namespace resource_name \
    remote_key registry_ref release_version contract_sha image; do
    [[ "${record_type}" == "RUNTIME" ]] || {
      recsys_error "unsupported Agent Registry runtime context: ${record_type}"
      return 2
    }
    if [[ "${artifact_kind}" == "mcp" ]]; then
      resource_kind="Deployment"
      resource_name="$(mcp_auth_active_workload "${remote_key}")"
    else
      resource_kind="SandboxAgent"
    fi

    expected_annotations="$(printf '%s\t%s\t%s' \
      "${registry_ref}" "${release_version}" "${contract_sha}")"
    actual_annotations="$(kubectl -n "${namespace}" get \
      "${resource_kind}" "${resource_name}" \
      -o go-template='{{ index .metadata.annotations "recsys.dev/agent-registry-ref" }}{{ "\t" }}{{ index .metadata.annotations "recsys.dev/agent-release-version" }}{{ "\t" }}{{ index .metadata.annotations "recsys.dev/contract-sha256" }}')"
    [[ "${actual_annotations}" == "${expected_annotations}" ]] || {
      recsys_error "runtime annotations do not match Registry lock for ${artifact_id}"
      return 2
    }

    actual_image="-"
    if [[ "${artifact_kind}" == "mcp" ]]; then
      actual_image="$(kubectl -n "${namespace}" get \
        "${resource_kind}" "${resource_name}" \
        -o jsonpath='{.spec.template.spec.containers[?(@.name=="mcp")].image}')"
      [[ "${actual_image}" == "${image}" ]] || {
        recsys_error "runtime image does not match Registry lock for ${artifact_id}"
        return 2
      }
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "${artifact_id}" "${resource_kind}" "${namespace}" "${resource_name}" \
      "${registry_ref}" "${release_version}" "${contract_sha}" "${actual_image}" \
      >>"${evidence_file}"
    record_count=$((record_count + 1))
  done <"${context_file}"

  ((record_count > 0)) || {
    recsys_error "Agent Registry lock contains no runtime records"
    return 2
  }
  recsys_log VERIFY "${record_count} runtime resources match the Registry lock"
}
