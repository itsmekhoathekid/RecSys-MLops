#!/usr/bin/env bash

# Non-secret MCP auth rotation metadata. Credentials never pass through this
# module; callers resolve only Kubernetes resource names and revision IDs.
mcp_auth_versions_file() {
  printf '%s\n' "${MCP_AUTH_VERSIONS_FILE:-configs/agentic/mcp-auth-versions.yaml}"
}

mcp_auth_versions_cli() {
  printf '%s\n' "${MCP_AUTH_VERSIONS_CLI:-ops/security/mcp_auth_versions.py}"
}

mcp_auth_validate() {
  python3 "$(mcp_auth_versions_cli)" validate "$(mcp_auth_versions_file)"
}

mcp_auth_chart_consumes_manifest() {
  case "$1" in
    feature-rag-mcp|context-agent|recommendation-mcp|recommendation-agent)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

mcp_auth_get() {
  local service="$1"
  local field="$2"
  python3 "$(mcp_auth_versions_cli)" get "${service}" "${field}" \
    --file "$(mcp_auth_versions_file)"
}

mcp_auth_active_revision() {
  mcp_auth_get "$1" activeRevision
}

mcp_auth_active_secret() {
  mcp_auth_get "$1" activeSecret
}

mcp_auth_active_workload() {
  mcp_auth_get "$1" activeWorkload
}

mcp_auth_active_url() {
  printf 'http://%s.kagent.svc.cluster.local:8080/mcp\n' \
    "$(mcp_auth_active_workload "$1")"
}

mcp_auth_list_deployed() {
  python3 "$(mcp_auth_versions_cli)" list-deployed "$1" \
    --file "$(mcp_auth_versions_file)"
}

mcp_auth_transition_base_ref() {
  printf '%s\n' "${MCP_AUTH_TRANSITION_BASE_REF:-${CI_BASE_REF:-HEAD^}}"
}

mcp_auth_transition() {
  local manifest base_ref previous result command_status=0
  manifest="$(mcp_auth_versions_file)"
  base_ref="$(mcp_auth_transition_base_ref)"
  git rev-parse --verify "${base_ref}^{commit}" >/dev/null 2>&1 || {
    printf 'cannot resolve MCP auth transition base ref: %s\n' "${base_ref}" >&2
    return 2
  }
  if ! git cat-file -e "${base_ref}:${manifest}" >/dev/null 2>&1; then
    printf 'bootstrap\n'
    return 0
  fi
  previous="$(mktemp)"
  git show "${base_ref}:${manifest}" >"${previous}"
  result="$(python3 "$(mcp_auth_versions_cli)" validate-transition \
    "${previous}" "${manifest}")" || command_status=$?
  rm -f -- "${previous}"
  ((command_status == 0)) || return "${command_status}"
  printf '%s\n' "${result}"
}

mcp_auth_transition_phase() {
  local service="$1"
  local transition
  transition="$(mcp_auth_transition)" || return
  case "${transition}" in
    bootstrap)
      printf 'bootstrap\n'
      ;;
    "${service}:"*)
      printf '%s\n' "${transition#*:}"
      ;;
    *)
      printf 'unchanged\n'
      ;;
  esac
}

mcp_auth_image_policy() {
  local unit_name="$1"
  local transition
  case "${unit_name}" in
    feature-rag-mcp|recommendation-mcp)
      transition="$(mcp_auth_transition)" || return
      if [[ "${transition}" == "unchanged" ]]; then
        printf 'release-artifact\n'
      else
        # A lifecycle-only release must preserve the exact image already
        # serving traffic. The component planner may still build an image
        # because the shared manifest selects both MCP components; deployment
        # deliberately ignores that artifact for the rotation transaction.
        printf 'installed-digest\n'
      fi
      ;;
    *)
      printf 'release-artifact\n'
      ;;
  esac
}

mcp_auth_previous_get() {
  local service="$1"
  local field="$2"
  local manifest base_ref previous command_status=0
  manifest="$(mcp_auth_versions_file)"
  base_ref="$(mcp_auth_transition_base_ref)"
  git rev-parse --verify "${base_ref}^{commit}" >/dev/null 2>&1 || return 2
  git cat-file -e "${base_ref}:${manifest}" >/dev/null 2>&1 || return 1
  previous="$(mktemp)"
  git show "${base_ref}:${manifest}" >"${previous}"
  python3 "$(mcp_auth_versions_cli)" get "${service}" "${field}" \
    --file "${previous}" || command_status=$?
  rm -f -- "${previous}"
  return "${command_status}"
}

mcp_auth_changed_revision() {
  local service="$1"
  local change="$2"
  local manifest base_ref previous command_status=0
  manifest="$(mcp_auth_versions_file)"
  base_ref="$(mcp_auth_transition_base_ref)"
  git rev-parse --verify "${base_ref}^{commit}" >/dev/null 2>&1 || {
    printf 'cannot resolve MCP auth transition base ref: %s\n' "${base_ref}" >&2
    return 2
  }
  previous="$(mktemp)"
  if git cat-file -e "${base_ref}:${manifest}" >/dev/null 2>&1; then
    git show "${base_ref}:${manifest}" >"${previous}"
  else
    printf '{}\n' >"${previous}"
  fi
  python3 - "${previous}" "${manifest}" "${service}" "${change}" <<'PY' || command_status=$?
import json
import sys

before_path, after_path, service_name, change = sys.argv[1:]
before = json.load(open(before_path, encoding="utf-8"))
after = json.load(open(after_path, encoding="utf-8"))
current = after["services"][service_name]
old_service = before.get("services", {}).get(service_name)

if change == "prepare":
    if old_service is None:
        names = [
            name
            for name, revision in current["revisions"].items()
            if name != current["activeRevision"]
            and name != "legacy"
            and revision["deploy"] is True
        ]
    else:
        names = sorted(set(current["revisions"]) - set(old_service["revisions"]))
elif change == "retire":
    if old_service is None:
        names = []
    else:
        names = [
            name
            for name in set(current["revisions"]) & set(old_service["revisions"])
            if old_service["revisions"][name]["deploy"] is True
            and current["revisions"][name]["deploy"] is False
        ]
else:
    raise SystemExit("unsupported revision change")

if len(names) != 1:
    raise SystemExit(
        f"expected exactly one {change} revision for {service_name}; found {len(names)}"
    )
print(names[0])
PY
  rm -f -- "${previous}"
  return "${command_status}"
}

mcp_auth_verify_prepare() {
  local service="$1"
  local smoke_function="$2"
  local phase candidate active mode candidate_workload
  phase="$(mcp_auth_transition_phase "${service}")" || return
  if [[ "${phase}" != "prepare" && "${phase}" != "bootstrap" ]]; then
    return 0
  fi
  candidate="$(mcp_auth_changed_revision "${service}" prepare)"
  active="$(mcp_auth_active_revision "${service}")"
  mode=distinct
  [[ "${active}" == "legacy" ]] && mode=same

  mkdir -p reports/agentic
  MCP_AUTH_MATRIX_EVIDENCE="reports/agentic/mcp-auth-prepare-${service}-${candidate}.json" \
    bash ops/validation/mcp_auth_rotation_matrix.sh \
      "${service}" "${active}" "${candidate}" "${mode}"
  candidate_workload="$(mcp_auth_get_revision_field \
    "${service}" "${candidate}" workloadName)"
  "${smoke_function}" "${candidate_workload}"
}

mcp_auth_get_revision_field() {
  local service="$1"
  local revision="$2"
  local field="$3"
  python3 - "$(mcp_auth_versions_file)" "${service}" "${revision}" "${field}" <<'PY'
import json
import sys

path, service, revision, field = sys.argv[1:]
payload = json.load(open(path, encoding="utf-8"))
value = payload["services"][service]["revisions"][revision][field]
if isinstance(value, (dict, list)):
    raise SystemExit("revision field must be scalar")
print(str(value).lower() if isinstance(value, bool) else value)
PY
}

mcp_auth_get_service_field() {
  local service="$1"
  local field="$2"
  python3 - "$(mcp_auth_versions_file)" "${service}" "${field}" <<'PY'
import json
import sys

path, service, field = sys.argv[1:]
payload = json.load(open(path, encoding="utf-8"))
value = payload["services"][service][field]
if isinstance(value, (dict, list)):
    raise SystemExit("service field must be scalar")
print(str(value).lower() if isinstance(value, bool) else value)
PY
}

mcp_auth_rollout_gate() {
  local service="$1"
  shift
  local mode="${1:-metadata-only}"
  local remote_mcp_server sandbox_agent
  (($# == 0)) || shift
  case "${service}" in
    featureRag)
      remote_mcp_server="recsys-feature-rag-mcp"
      sandbox_agent="recsys-context-agent-sandbox"
      ;;
    recommendation)
      remote_mcp_server="recsys-recommendation-mcp"
      sandbox_agent="recsys-recommendation-agent-sandbox"
      ;;
    *)
      printf 'unsupported MCP auth service: %s\n' "${service}" >&2
      return 2
      ;;
  esac

  local args=(
    --namespace kagent
    --remote-mcp-server "${remote_mcp_server}"
    --sandbox-agent "${sandbox_agent}"
    --expected-revision "$(mcp_auth_active_revision "${service}")"
    --expected-url "$(mcp_auth_active_url "${service}")"
    --expected-secret "$(mcp_auth_active_secret "${service}")"
  )
  if [[ "${mode}" == "metadata-only" ]]; then
    args+=(--metadata-only)
  elif [[ "${mode}" == "smoke" && $# -gt 0 ]]; then
    args+=(-- "$@")
  else
    printf 'rollout gate mode must be metadata-only or smoke with a command\n' >&2
    return 2
  fi
  if [[ -n "${MCP_AUTH_GATE_EVIDENCE:-}" ]]; then
    [[ ! -e "${MCP_AUTH_GATE_EVIDENCE}" ]] || {
      printf 'refusing to overwrite rollout-gate evidence: %s\n' \
        "${MCP_AUTH_GATE_EVIDENCE}" >&2
      return 2
    }
    mkdir -p "$(dirname "${MCP_AUTH_GATE_EVIDENCE}")"
    bash ops/validation/mcp_auth_rotation_gate.sh "${args[@]}" \
      | tee "${MCP_AUTH_GATE_EVIDENCE}"
  else
    bash ops/validation/mcp_auth_rotation_gate.sh "${args[@]}"
  fi
}
