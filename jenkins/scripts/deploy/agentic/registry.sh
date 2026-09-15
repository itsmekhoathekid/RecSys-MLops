#!/usr/bin/env bash

# Transport-only adapter. Manifest, dependency, checksum, read-back and lock
# semantics live in jenkins.python.agent_registry_release.

agentic_registry_port_forward_pid=""

agentic_registry_open_tunnel() {
  local local_port="${AGENT_REGISTRY_LOCAL_PORT:-12121}"
  local log_file="reports/agentic/agentregistry-port-forward.log"
  mkdir -p reports/agentic
  [[ -z "${agentic_registry_port_forward_pid}" ]] || return 0
  kubectl -n "${AGENT_REGISTRY_NAMESPACE:-agentregistry}" port-forward \
    service/agentregistry "${local_port}:12121" >"${log_file}" 2>&1 &
  agentic_registry_port_forward_pid=$!
  recsys_wait_http "http://127.0.0.1:${local_port}/openapi.json" 30 1 \
    "${agentic_registry_port_forward_pid}"
  arctl configure --url "http://127.0.0.1:${local_port}" >/dev/null
}

agentic_registry_close_tunnel() {
  if [[ -n "${agentic_registry_port_forward_pid}" ]]; then
    recsys_cleanup_process "${agentic_registry_port_forward_pid}"
    agentic_registry_port_forward_pid=""
  fi
}

agentic_registry_git_url() {
  local remote
  remote="${AGENT_REGISTRY_GIT_URL:-$(git config --get remote.origin.url)}"
  [[ -n "${remote}" ]] || {
    recsys_error "Agent Registry publish requires AGENT_REGISTRY_GIT_URL or origin"
    return 2
  }
  printf '%s\n' "${remote}"
}

agentic_assert_registry_publish_branch() {
  local branch="${BRANCH_NAME:-${GIT_BRANCH:-$(git branch --show-current)}}"
  case "${branch}" in
    main|origin/main|refs/heads/main|refs/remotes/origin/main) ;;
    *)
      if ! recsys_is_true "${DEPLOY_PULL_REQUESTS:-0}" \
        && ! recsys_is_true "${FORCE_DEPLOY:-0}" \
        && { ! git rev-parse --verify 'origin/main^{commit}' >/dev/null 2>&1 \
          || [[ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]]; }; then
        recsys_error "Agent Registry publication is not authorized on ${branch:-detached}"
        return 2
      fi
      ;;
  esac
  command -v arctl >/dev/null 2>&1 || {
    recsys_error "arctl is required for Agent Registry publication"
    return 2
  }
}

agentic_registry_installed_image() {
  local workload_unit="$1"
  local value_path="$2"
  local release=""
  local namespace=""
  local record_type value_a value_b value_c _value_d
  while IFS=$'\t' read -r record_type value_a value_b value_c _value_d; do
    if [[ "${record_type}" == "UNIT" ]]; then
      release="${value_b}"
      namespace="${value_c}"
      break
    fi
  done < <(
    # plan_path is owned by the release-unit entrypoint that sources this adapter.
    # shellcheck disable=SC2154
    python3 jenkins/python/release_plan.py deploy-context \
      "${workload_unit}" --plan "${plan_path}"
  )
  [[ -n "${release}" && -n "${namespace}" ]] || {
    recsys_error "cannot resolve installed image owner for ${workload_unit}"
    return 2
  }
  helm get values "${release}" -n "${namespace}" -o json 2>/dev/null \
    | python3 -c '
import json, sys
value = json.load(sys.stdin)
for token in sys.argv[1].split("."):
    value = value.get(token, {}) if isinstance(value, dict) else {}
print(value if isinstance(value, str) else "")
' "${value_path}"
}

agentic_registry_effective_image() {
  local workload_unit="$1"
  local image_name="$2"
  local value_path="$3"
  local policy reference
  policy="$(mcp_auth_image_policy "${workload_unit}")"
  if [[ "${policy}" == "installed-digest" ]]; then
    reference="$(agentic_registry_installed_image "${workload_unit}" "${value_path}")"
    if [[ -z "${reference}" ]]; then
      reference="$(image_manifest_lookup "${image_name}")"
    fi
  else
    reference="$(image_manifest_lookup "${image_name}")"
  fi
  [[ "${reference}" =~ @sha256:[0-9a-f]{64}$ ]] || {
    recsys_error "Agent Registry publication requires immutable ${image_name} digest"
    return 2
  }
  printf '%s' "${reference}"
}

publish_agent_registry_artifact() {
  local artifact_id="$1"
  local commit="${GIT_COMMIT:-$(git rev-parse HEAD)}"
  local kind registry_name tag workload_unit chart_artifact
  local image_name=""
  local image_value=""
  local image_reference=""
  local remote_key=""
  local remote_url=""
  local record_type value_a value_b value_c
  local manifest_dir=".ci-deploy/agent-registry-manifests"
  local readback_dir=".ci-deploy/agent-registry-readbacks"
  local manifest readback
  local -a render_args=()

  agentic_assert_registry_publish_branch
  while IFS=$'\t' read -r record_type value_a value_b value_c; do
    case "${record_type}" in
      RESOURCE)
        kind="${value_a}"
        registry_name="${value_b}"
        tag="${value_c}"
        ;;
      WORKLOAD)
        workload_unit="${value_a}"
        chart_artifact="${value_b}"
        ;;
      IMAGE)
        image_name="${value_a}"
        image_value="${value_b}"
        ;;
      REMOTE)
        remote_key="${value_a}"
        ;;
      *)
        recsys_error "unsupported Agent Registry artifact context: ${record_type}"
        return 2
        ;;
    esac
  done < <(
    python3 -m jenkins.python.agent_registry_release artifact-context \
      "${artifact_id}" --commit "${commit}"
  )
  [[ -n "${kind}" && -n "${registry_name}" && -n "${tag}" \
    && -n "${workload_unit}" && -n "${chart_artifact}" ]] || {
    recsys_error "Agent Registry artifact context is incomplete for ${artifact_id}"
    return 2
  }

  if [[ -n "${image_name}" ]]; then
    image_reference="$(
      agentic_registry_effective_image \
        "${workload_unit}" "${image_name}" "${image_value}"
    )"
    render_args+=(--image "${image_name}=${image_reference}")
  fi
  if [[ -n "${remote_key}" ]]; then
    remote_url="$(mcp_auth_active_url "${remote_key}")"
    render_args+=(--remote-url "${remote_key}=${remote_url}")
  fi

  mkdir -p "${manifest_dir}" "${readback_dir}"
  manifest="${manifest_dir}/${artifact_id}.json"
  readback="${readback_dir}/${artifact_id}.json"
  python3 -m jenkins.python.agent_registry_release validate-dependencies \
    "${artifact_id}" \
    --commit "${commit}" \
    --manifest-dir "${manifest_dir}" \
    --readback-dir "${readback_dir}"
  python3 -m jenkins.python.agent_registry_release render \
    "${artifact_id}" \
    --commit "${commit}" \
    --git-url "$(agentic_registry_git_url)" \
    --output "${manifest}" \
    "${render_args[@]}"

  agentic_registry_open_tunnel
  if arctl get "${kind}" "${registry_name}" --tag "${tag}" -o json \
    >"${readback}" 2>/dev/null; then
    python3 -m jenkins.python.agent_registry_release validate-readback \
      --expected "${manifest}" --readback "${readback}"
    recsys_log DEPLOY "Agent Registry ${registry_name}@${tag} already matches"
  else
    arctl apply -f "${manifest}"
    arctl get "${kind}" "${registry_name}" --tag "${tag}" -o json \
      >"${readback}"
    python3 -m jenkins.python.agent_registry_release validate-readback \
      --expected "${manifest}" --readback "${readback}"
    recsys_log DEPLOY "published and read back ${registry_name}@${tag}"
  fi
}

verify_agent_registry_runtime_lock() {
  local plan_file="$1"
  local lock_file="${2:-.ci-deploy/agent-registry-lock.json}"
  local evidence_file=".ci-deploy/agent-registry-runtime-verification.tsv"
  local context_file=".ci-deploy/agent-registry-runtime-context.tsv"
  local record_type artifact_id artifact_kind workload_unit namespace
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
    record_type artifact_id artifact_kind workload_unit namespace resource_name \
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
