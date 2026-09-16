#!/usr/bin/env bash

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
      REMOTE) remote_key="${value_a}" ;;
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
    recsys_log PUBLISH "Agent Registry ${registry_name}@${tag} already matches"
  else
    arctl apply -f "${manifest}"
    arctl get "${kind}" "${registry_name}" --tag "${tag}" -o json \
      >"${readback}"
    python3 -m jenkins.python.agent_registry_release validate-readback \
      --expected "${manifest}" --readback "${readback}"
    recsys_log PUBLISH "published and read back ${registry_name}@${tag}"
  fi
}
