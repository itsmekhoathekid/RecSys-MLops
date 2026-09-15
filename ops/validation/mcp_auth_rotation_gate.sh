#!/usr/bin/env bash
set -Eeuo pipefail

case "$-" in
  *x*)
    printf 'ERROR: refusing to run MCP auth validation while shell xtrace is enabled\n' >&2
    exit 2
    ;;
esac

usage() {
  cat <<'USAGE'
Usage:
  mcp_auth_rotation_gate.sh \
    --remote-mcp-server NAME \
    --sandbox-agent NAME \
    --expected-revision REVISION \
    [--namespace NAMESPACE] \
    [--expected-url URL] \
    [--expected-secret SECRET_NAME] \
    [--timeout-seconds SECONDS] \
    [--poll-seconds SECONDS] \
    [--retire-template ACTOR_TEMPLATE]... \
    [--substrate-status-url URL] \
    [--controller-service SERVICE] \
    [--controller-local-port PORT] \
    [--substrate-namespace NAMESPACE] \
    [--valkey-pod POD] \
    [--valkey-service SERVICE] \
    [--metadata-only] \
    [-- SMOKE_COMMAND [ARG ...]]

Waits for all of the following without mutating cluster resources:
  * RemoteMCPServer status.observedGeneration matches metadata.generation.
  * RemoteMCPServer Accepted is True for the expected auth revision.
  * SandboxAgent has observed the expected literal auth revision.
  * An ActorTemplate for the current SandboxAgent generation is Ready.

Without a command, the script prints only non-secret rollout metadata as JSON.
When a command follows `--`, it runs after the gate with a newly generated
session identifier in RECSYS_FRESH_SESSION_ID. The smoke command must use that
identifier so the request cannot resume an actor born from an older template,
then write {"contextIds":[...]} to MCP_AUTH_ROTATION_SESSION_EVIDENCE. The gate
reproduces kagent rc1's deterministic session-to-actor ID function and fails
closed unless every exact smoke actor is new and bound to the desired
ActorTemplate. Unrelated concurrent actors cannot satisfy this attestation.
If the controller's global actor inventory is unavailable because a legacy
record cannot be decoded, the smoke gate reads only each exact expected actor
key from Substrate's Valkey store and additionally requires its createTime to
be at or after the smoke start. Retirement never uses this fallback: it still
requires a complete global inventory and therefore remains fail-closed.
Use --metadata-only to explicitly forbid execution of a smoke command.

Each --retire-template enables a fail-closed, read-only drain check through
kagent's /api/substrate/status endpoint. The check rejects retirement while any
non-golden actor still names that birth template. Without
--substrate-status-url, the helper opens a temporary kubectl port-forward to the
controller Service; neither path deletes actors or ActorTemplates.

Environment equivalents:
  MCP_AUTH_ROTATION_NAMESPACE
  MCP_AUTH_ROTATION_REMOTE_MCP_SERVER
  MCP_AUTH_ROTATION_SANDBOX_AGENT
  MCP_AUTH_ROTATION_EXPECTED_REVISION
  MCP_AUTH_ROTATION_EXPECTED_URL
  MCP_AUTH_ROTATION_EXPECTED_SECRET
  MCP_AUTH_ROTATION_TIMEOUT_SECONDS
  MCP_AUTH_ROTATION_POLL_SECONDS
  MCP_AUTH_ROTATION_SUBSTRATE_STATUS_URL
  MCP_AUTH_ROTATION_SUBSTRATE_AUTHORIZATION
  MCP_AUTH_ROTATION_CONTROLLER_SERVICE
  MCP_AUTH_ROTATION_CONTROLLER_LOCAL_PORT
  MCP_AUTH_ROTATION_SUBSTRATE_NAMESPACE
  MCP_AUTH_ROTATION_VALKEY_POD
  MCP_AUTH_ROTATION_VALKEY_SERVICE
USAGE
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 2
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

is_positive_integer() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

namespace="${MCP_AUTH_ROTATION_NAMESPACE:-kagent}"
remote_mcp_server="${MCP_AUTH_ROTATION_REMOTE_MCP_SERVER:-}"
sandbox_agent="${MCP_AUTH_ROTATION_SANDBOX_AGENT:-}"
expected_revision="${MCP_AUTH_ROTATION_EXPECTED_REVISION:-}"
expected_url="${MCP_AUTH_ROTATION_EXPECTED_URL:-}"
expected_secret="${MCP_AUTH_ROTATION_EXPECTED_SECRET:-}"
timeout_seconds="${MCP_AUTH_ROTATION_TIMEOUT_SECONDS:-600}"
poll_seconds="${MCP_AUTH_ROTATION_POLL_SECONDS:-2}"
substrate_status_url="${MCP_AUTH_ROTATION_SUBSTRATE_STATUS_URL:-}"
substrate_authorization="${MCP_AUTH_ROTATION_SUBSTRATE_AUTHORIZATION:-}"
unset MCP_AUTH_ROTATION_SUBSTRATE_AUTHORIZATION
controller_service="${MCP_AUTH_ROTATION_CONTROLLER_SERVICE:-kagent-controller}"
controller_local_port="${MCP_AUTH_ROTATION_CONTROLLER_LOCAL_PORT:-18083}"
substrate_namespace="${MCP_AUTH_ROTATION_SUBSTRATE_NAMESPACE:-ate-system}"
valkey_pod="${MCP_AUTH_ROTATION_VALKEY_POD:-valkey-cluster-0}"
valkey_service="${MCP_AUTH_ROTATION_VALKEY_SERVICE:-valkey-cluster-service}"
metadata_only=false
smoke_command=()
retire_templates=()
port_forward_pid=""
port_forward_log=""
substrate_auth_header_file=""
session_evidence_file=""

cleanup() {
  if [[ -n "${port_forward_pid}" ]]; then
    kill "${port_forward_pid}" >/dev/null 2>&1 || true
    wait "${port_forward_pid}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${port_forward_log}" ]]; then
    rm -f "${port_forward_log}"
  fi
  if [[ -n "${substrate_auth_header_file}" ]]; then
    rm -f "${substrate_auth_header_file}"
  fi
  if [[ -n "${session_evidence_file}" ]]; then
    rm -f "${session_evidence_file}"
  fi
}
trap cleanup EXIT

while (($# > 0)); do
  case "$1" in
    --namespace)
      (($# >= 2)) || die "--namespace requires a value"
      namespace="$2"
      shift 2
      ;;
    --remote-mcp-server)
      (($# >= 2)) || die "--remote-mcp-server requires a value"
      remote_mcp_server="$2"
      shift 2
      ;;
    --sandbox-agent)
      (($# >= 2)) || die "--sandbox-agent requires a value"
      sandbox_agent="$2"
      shift 2
      ;;
    --expected-revision)
      (($# >= 2)) || die "--expected-revision requires a value"
      expected_revision="$2"
      shift 2
      ;;
    --expected-url)
      (($# >= 2)) || die "--expected-url requires a value"
      expected_url="$2"
      shift 2
      ;;
    --expected-secret)
      (($# >= 2)) || die "--expected-secret requires a value"
      expected_secret="$2"
      shift 2
      ;;
    --timeout-seconds)
      (($# >= 2)) || die "--timeout-seconds requires a value"
      timeout_seconds="$2"
      shift 2
      ;;
    --poll-seconds)
      (($# >= 2)) || die "--poll-seconds requires a value"
      poll_seconds="$2"
      shift 2
      ;;
    --retire-template)
      (($# >= 2)) || die "--retire-template requires a value"
      retire_templates+=("$2")
      shift 2
      ;;
    --substrate-status-url)
      (($# >= 2)) || die "--substrate-status-url requires a value"
      substrate_status_url="$2"
      shift 2
      ;;
    --controller-service)
      (($# >= 2)) || die "--controller-service requires a value"
      controller_service="$2"
      shift 2
      ;;
    --controller-local-port)
      (($# >= 2)) || die "--controller-local-port requires a value"
      controller_local_port="$2"
      shift 2
      ;;
    --substrate-namespace)
      (($# >= 2)) || die "--substrate-namespace requires a value"
      substrate_namespace="$2"
      shift 2
      ;;
    --valkey-pod)
      (($# >= 2)) || die "--valkey-pod requires a value"
      valkey_pod="$2"
      shift 2
      ;;
    --valkey-service)
      (($# >= 2)) || die "--valkey-service requires a value"
      valkey_service="$2"
      shift 2
      ;;
    --metadata-only)
      metadata_only=true
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    --)
      shift
      smoke_command=("$@")
      break
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "${remote_mcp_server}" ]] || die "--remote-mcp-server is required"
[[ -n "${sandbox_agent}" ]] || die "--sandbox-agent is required"
[[ -n "${expected_revision}" ]] || die "--expected-revision is required"
is_positive_integer "${timeout_seconds}" || die "--timeout-seconds must be a positive integer"
is_positive_integer "${poll_seconds}" || die "--poll-seconds must be a positive integer"
is_positive_integer "${controller_local_port}" || die "--controller-local-port must be a positive integer"
if [[ "${metadata_only}" == true && ${#smoke_command[@]} -gt 0 ]]; then
  die "--metadata-only cannot be combined with a smoke command"
fi

require_command kubectl
require_command jq
if ((${#retire_templates[@]} > 0 || ${#smoke_command[@]} > 0)); then
  require_command curl
fi

deadline=$((SECONDS + timeout_seconds))
last_summary="resources not observed yet"
rms_json=""
sandbox_json=""
actor_template_json=""

while ((SECONDS < deadline)); do
  rms_json="$(kubectl -n "${namespace}" get \
    "remotemcpservers.kagent.dev/${remote_mcp_server}" -o json 2>/dev/null || true)"
  sandbox_json="$(kubectl -n "${namespace}" get \
    "sandboxagents.kagent.dev/${sandbox_agent}" -o json 2>/dev/null || true)"

  if [[ -z "${rms_json}" || -z "${sandbox_json}" ]]; then
    last_summary="waiting for RemoteMCPServer or SandboxAgent to exist"
    sleep "${poll_seconds}"
    continue
  fi

  rms_generation="$(jq -r '.metadata.generation // 0' <<<"${rms_json}")"
  rms_observed_generation="$(jq -r '.status.observedGeneration // 0' <<<"${rms_json}")"
  rms_accepted="$(jq -r '[.status.conditions[]? | select(.type == "Accepted")][-1].status // ""' <<<"${rms_json}")"
  rms_revision="$(jq -r '.metadata.annotations["recsys.ai/mcp-auth-revision"] // ""' <<<"${rms_json}")"
  rms_url="$(jq -r '.spec.url // ""' <<<"${rms_json}")"
  rms_secret="$(jq -r '[.spec.headersFrom[]? | select(.name == "Authorization")][-1].valueFrom.name // ""' <<<"${rms_json}")"

  sandbox_generation="$(jq -r '.metadata.generation // 0' <<<"${sandbox_json}")"
  sandbox_observed_generation="$(jq -r '.status.observedGeneration // 0' <<<"${sandbox_json}")"
  sandbox_accepted="$(jq -r '[.status.conditions[]? | select(.type == "Accepted")][-1].status // ""' <<<"${sandbox_json}")"
  sandbox_revision="$(jq -r '[.spec.declarative.deployment.env[]? | select(.name == "RECSYS_MCP_AUTH_REVISION")][-1].value // ""' <<<"${sandbox_json}")"
  sandbox_annotation_revision="$(jq -r '.metadata.annotations["recsys.ai/mcp-auth-revision"] // ""' <<<"${sandbox_json}")"

  revision_ready=false
  if [[ "${rms_revision}" == "${expected_revision}" &&
        "${sandbox_revision}" == "${expected_revision}" &&
        "${sandbox_annotation_revision}" == "${expected_revision}" ]]; then
    revision_ready=true
  fi

  endpoint_ready=true
  if [[ -n "${expected_url}" && "${rms_url}" != "${expected_url}" ]]; then
    endpoint_ready=false
  fi
  if [[ -n "${expected_secret}" && "${rms_secret}" != "${expected_secret}" ]]; then
    endpoint_ready=false
  fi

  actor_templates_json="$(kubectl -n "${namespace}" get actortemplates.ate.dev \
    -l "kagent.dev/sandbox-agent=${sandbox_agent}" -o json 2>/dev/null || true)"
  actor_template_json=""
  actor_template_candidate_count=0
  if [[ -n "${actor_templates_json}" ]]; then
    actor_template_candidates="$(jq -c --arg generation "${sandbox_generation}" '
      [.items[]
       | select(.metadata.deletionTimestamp == null)
       | select(.metadata.annotations["kagent.dev/desired-generation"] == $generation)
       | select(.status.phase == "Ready")]
    ' <<<"${actor_templates_json}")"
    actor_template_candidate_count="$(jq 'length' <<<"${actor_template_candidates}")"
    if ((actor_template_candidate_count == 1)); then
      actor_template_json="$(jq -c '.[0]' <<<"${actor_template_candidates}")"
    fi
  fi

  if [[ "${rms_generation}" == "${rms_observed_generation}" &&
        "${rms_accepted}" == "True" &&
        "${sandbox_generation}" == "${sandbox_observed_generation}" &&
        "${sandbox_accepted}" == "True" &&
        "${revision_ready}" == true &&
        "${endpoint_ready}" == true &&
        -n "${actor_template_json}" ]]; then
    break
  fi

  actor_phase="missing"
  if [[ -n "${actor_template_json}" ]]; then
    actor_phase="$(jq -r '.status.phase // "missing"' <<<"${actor_template_json}")"
  fi
  last_summary="rms=${rms_observed_generation}/${rms_generation} accepted=${rms_accepted:-unset} revision=${rms_revision:-unset}; sandbox=${sandbox_observed_generation}/${sandbox_generation} accepted=${sandbox_accepted:-unset} revision=${sandbox_revision:-unset}; current-template=${actor_phase} candidates=${actor_template_candidate_count}"
  sleep "${poll_seconds}"
done

if [[ -z "${actor_template_json}" ||
      "${rms_generation:-0}" != "${rms_observed_generation:-0}" ||
      "${rms_accepted:-}" != "True" ||
      "${sandbox_generation:-0}" != "${sandbox_observed_generation:-0}" ||
      "${sandbox_accepted:-}" != "True" ||
      "${rms_revision:-}" != "${expected_revision}" ||
      "${sandbox_revision:-}" != "${expected_revision}" ||
      "${sandbox_annotation_revision:-}" != "${expected_revision}" ||
      "${endpoint_ready:-false}" != true ]]; then
  printf 'Timed out after %ss: %s\n' "${timeout_seconds}" "${last_summary}" >&2
  exit 1
fi

# Close the small read race between selecting a Ready template and reporting
# success: the current SandboxAgent generation must still be the one selected.
final_sandbox_generation="$(kubectl -n "${namespace}" get \
  "sandboxagents.kagent.dev/${sandbox_agent}" -o jsonpath='{.metadata.generation}')"
if [[ "${final_sandbox_generation}" != "${sandbox_generation}" ]]; then
  printf 'SandboxAgent generation changed during validation (%s -> %s); retry the gate.\n' \
    "${sandbox_generation}" "${final_sandbox_generation}" >&2
  exit 1
fi

actor_template_name="$(jq -r '.metadata.name' <<<"${actor_template_json}")"
actor_template_uid="$(jq -r '.metadata.uid // ""' <<<"${actor_template_json}")"
actor_template_hash="$(jq -r '.metadata.annotations["kagent.dev/actor-template-hash"] // ""' <<<"${actor_template_json}")"
actor_template_created_at="$(jq -r '.metadata.creationTimestamp // ""' <<<"${actor_template_json}")"
golden_actor_id="$(jq -r '.status.goldenActorID // ""' <<<"${actor_template_json}")"
rms_uid="$(jq -r '.metadata.uid // ""' <<<"${rms_json}")"
sandbox_uid="$(jq -r '.metadata.uid // ""' <<<"${sandbox_json}")"
secret_uid=""
secret_resource_version=""
if [[ -n "${expected_secret}" ]]; then
  IFS=$'\t' read -r secret_uid secret_resource_version < <(
    kubectl -n "${namespace}" get "secret/${expected_secret}" \
      -o jsonpath='{.metadata.uid}{"\t"}{.metadata.resourceVersion}{"\n"}'
  )
  [[ -n "${secret_uid}" && -n "${secret_resource_version}" ]] || \
    die "expected Secret metadata is incomplete: ${expected_secret}"
fi
workload_name=""
workload_uid=""
workload_generation=0
if [[ "${expected_url}" =~ ^http://([a-z0-9-]+)\.${namespace}\.svc\.cluster\.local:8080/mcp$ ]]; then
  workload_name="${BASH_REMATCH[1]}"
  workload_json="$(kubectl -n "${namespace}" get \
    "deployment/${workload_name}" -o json)"
  workload_uid="$(jq -r '.metadata.uid // ""' <<<"${workload_json}")"
  workload_generation="$(jq -r '.metadata.generation // 0' <<<"${workload_json}")"
fi
fresh_session_id=""
smoke_executed=false
smoke_attested=false
smoke_actor_count=0
smoke_attestation_source=""
retirement_checked=false
retirement_templates_json='[]'

fetch_substrate_status() {
  local separator='?'
  local curl_args=(-fsS --max-time 15)
  if [[ "${substrate_status_url}" == *\?* ]]; then
    separator='&'
  fi
  if [[ -n "${substrate_auth_header_file}" ]]; then
    curl_args+=(--header "@${substrate_auth_header_file}")
  fi
  curl "${curl_args[@]}" \
    "${substrate_status_url}${separator}namespace=${namespace}"
}

substrate_status_is_complete() {
  jq -e '
    .error == false and
    .data.enabled == true and
    ((.data.ateApiError // "") == "") and
    ((.data.actors | type) == "array")
  ' >/dev/null <<<"$1"
}

fetch_substrate_actor_by_id() {
  local actor_id="$1"
  [[ "${actor_id}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || \
    die "refusing to query invalid actor ID"
  kubectl -n "${substrate_namespace}" exec "${valkey_pod}" -- \
    valkey-cli -h "${valkey_service}" -c --raw \
    GET "actor:${namespace}:${actor_id}"
}

if ((${#retire_templates[@]} > 0)); then
  for retire_template in "${retire_templates[@]}"; do
    [[ -n "${retire_template}" ]] || die "--retire-template cannot be empty"
    if [[ "${retire_template}" == "${actor_template_name}" ]]; then
      die "refusing to mark the current desired ActorTemplate ${retire_template} as retired"
    fi
    retire_template_json="$(kubectl -n "${namespace}" get \
      "actortemplates.ate.dev/${retire_template}" -o json 2>/dev/null || true)"
    [[ -n "${retire_template_json}" ]] || die "retirement ActorTemplate does not exist: ${retire_template}"
    retire_template_owner="$(jq -r '.metadata.labels["kagent.dev/sandbox-agent"] // ""' \
      <<<"${retire_template_json}")"
    [[ "${retire_template_owner}" == "${sandbox_agent}" ]] || \
      die "ActorTemplate ${retire_template} is not owned by SandboxAgent ${sandbox_agent}"
    retire_template_generation="$(jq -r \
      '.metadata.annotations["kagent.dev/desired-generation"] // ""' \
      <<<"${retire_template_json}")"
    [[ "${retire_template_generation}" =~ ^[0-9]+$ ]] || \
      die "ActorTemplate ${retire_template} has no valid desired-generation annotation"
    if ((10#${retire_template_generation} >= 10#${sandbox_generation})); then
      die "ActorTemplate ${retire_template} is not older than SandboxAgent generation ${sandbox_generation}"
    fi
    if [[ "$(jq -r '.metadata.deletionTimestamp // ""' <<<"${retire_template_json}")" != "" ]]; then
      die "ActorTemplate ${retire_template} is already terminating; drain cannot be proven"
    fi
  done
fi

if ((${#retire_templates[@]} > 0 || ${#smoke_command[@]} > 0)); then
  if [[ -n "${substrate_authorization}" ]]; then
    if [[ "${substrate_authorization}" == *$'\n'* ||
          "${substrate_authorization}" == *$'\r'* ]]; then
      die "MCP_AUTH_ROTATION_SUBSTRATE_AUTHORIZATION cannot contain newlines"
    fi
    substrate_auth_header_file="$(mktemp "${TMPDIR:-/tmp}/mcp-auth-rotation-auth.XXXXXX")"
    chmod 0600 "${substrate_auth_header_file}"
    printf 'Authorization: %s\n' "${substrate_authorization}" >"${substrate_auth_header_file}"
  fi

  if [[ -z "${substrate_status_url}" ]]; then
    port_forward_log="$(mktemp "${TMPDIR:-/tmp}/mcp-auth-rotation-port-forward.XXXXXX")"
    kubectl -n "${namespace}" port-forward "service/${controller_service}" \
      "${controller_local_port}:8083" >"${port_forward_log}" 2>&1 &
    port_forward_pid=$!
    substrate_status_url="http://127.0.0.1:${controller_local_port}/api/substrate/status"

    port_forward_ready=false
    for ((attempt = 0; attempt < 30; attempt++)); do
      if ! kill -0 "${port_forward_pid}" >/dev/null 2>&1; then
        sed -n '1,80p' "${port_forward_log}" >&2
        die "kagent controller port-forward exited before the substrate status API became ready"
      fi
      if fetch_substrate_status >/dev/null 2>&1; then
        port_forward_ready=true
        break
      fi
      sleep 1
    done
    [[ "${port_forward_ready}" == true ]] || die "timed out waiting for the substrate status API port-forward"
  fi
fi

if ((${#smoke_command[@]} > 0)); then
  substrate_status_before_smoke="$(fetch_substrate_status 2>/dev/null || true)"
  actor_ids_before_smoke='[]'
  inventory_complete_before_smoke=false
  if [[ -n "${substrate_status_before_smoke}" ]] && \
     substrate_status_is_complete "${substrate_status_before_smoke}"; then
    inventory_complete_before_smoke=true
    actor_ids_before_smoke="$(jq -c \
      --arg namespace "${namespace}" '
        [.data.actors[]
         | select(.atespace == $namespace)
         | select(.atespace != "ate-golden")
         | .actorId
         | select(type == "string" and length > 0)]
        | unique
      ' <<<"${substrate_status_before_smoke}")"
  else
    printf 'Global actor inventory is incomplete; fresh smoke will use exact-key attestation.\n' >&2
  fi

  revision_slug="$(printf '%s' "${expected_revision}" | tr -cs '[:alnum:].-' '-')"
  revision_slug="${revision_slug#-}"
  revision_slug="${revision_slug%-}"
  [[ -n "${revision_slug}" ]] || revision_slug="revision"
  fresh_session_id="mcp-auth-${revision_slug}-$(date -u '+%Y%m%d%H%M%S')-$$"
  smoke_started_epoch="$(date -u '+%s')"
  session_evidence_file="$(mktemp "${TMPDIR:-/tmp}/mcp-auth-session-evidence.XXXXXX")"
  chmod 0600 "${session_evidence_file}"
  printf 'Running fresh-session smoke for %s/%s with session %s\n' \
    "${namespace}" "${sandbox_agent}" "${fresh_session_id}" >&2
  RECSYS_FRESH_SESSION_ID="${fresh_session_id}" \
  MCP_AUTH_ROTATION_NAMESPACE="${namespace}" \
  MCP_AUTH_ROTATION_REMOTE_MCP_SERVER="${remote_mcp_server}" \
  MCP_AUTH_ROTATION_SANDBOX_AGENT="${sandbox_agent}" \
  MCP_AUTH_ROTATION_EXPECTED_REVISION="${expected_revision}" \
  MCP_AUTH_ROTATION_ACTOR_TEMPLATE="${actor_template_name}" \
  MCP_AUTH_ROTATION_SESSION_EVIDENCE="${session_evidence_file}" \
    "${smoke_command[@]}" >&2
  smoke_executed=true

  session_context_ids_json="$(jq -ce \
    --arg prefix "${fresh_session_id}" '
      select(type == "object")
      | select((keys | sort) == ["contextIds"])
      | .contextIds
      | select(type == "array" and length > 0)
      | select(all(.[];
          type == "string" and length > 0 and
          (. == $prefix or startswith($prefix + "-"))))
      | select(length == (unique | length))
    ' "${session_evidence_file}")" || \
    die "smoke command did not attest its exact fresh contextId values"
  expected_actor_ids_json="$(python3 - \
    "${namespace}" "${sandbox_agent}" "${sandbox_uid}" \
    "${session_evidence_file}" <<'PY'
import hashlib
import json
import re
import sys

namespace, sandbox_name, sandbox_uid, evidence_path = sys.argv[1:]
context_ids = json.load(open(evidence_path, encoding="utf-8"))["contextIds"]
dns1123 = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")

prefix = f"asr-{namespace}-{sandbox_name}".lower().replace("_", "-")
if len(prefix) > 63:
    prefix = prefix[:63].rstrip("-")
if dns1123.fullmatch(prefix) is None:
    prefix = f"asr-{sandbox_uid}"[:63]

actor_ids = []
for session_id in context_ids:
    trimmed = session_id.strip()
    sanitized = trimmed.lower().replace("_", "-")
    raw = f"{prefix}-{sanitized}".lower().replace("_", "-")
    if len(raw) <= 63 and dns1123.fullmatch(raw) is not None:
        actor_id = raw
    else:
        digest = hashlib.sha256(
            f"{namespace}/{sandbox_name}/{trimmed}".encode()
        ).hexdigest()[:24]
        actor_id = f"asr-{digest}"
    actor_ids.append(actor_id)

if len(actor_ids) != len(set(actor_ids)):
    raise SystemExit("fresh contextIds map to duplicate actor IDs")
print(json.dumps(actor_ids, separators=(",", ":")))
PY
)" || die "failed to derive exact rc1 actor IDs from smoke contextIds"
  if [[ "${inventory_complete_before_smoke}" == true ]]; then
    preexisting_expected_actor_count="$(jq \
      --argjson expected "${expected_actor_ids_json}" \
      '[.[] | . as $actorId | select(($expected | index($actorId)) != null)] | length' \
      <<<"${actor_ids_before_smoke}")"
    ((preexisting_expected_actor_count == 0)) || \
      die "fresh smoke contextId resolved to an actor that existed before smoke"
  fi

  smoke_attestation_deadline=$((SECONDS + timeout_seconds))
  smoke_new_actor_count=0
  smoke_wrong_template_count=0
  smoke_expected_actor_count="$(jq 'length' <<<"${expected_actor_ids_json}")"
  while ((SECONDS < smoke_attestation_deadline)); do
    substrate_status_after_smoke="$(fetch_substrate_status 2>/dev/null || true)"
    if [[ "${inventory_complete_before_smoke}" == true ]] && \
       [[ -n "${substrate_status_after_smoke}" ]] && \
       substrate_status_is_complete "${substrate_status_after_smoke}"; then
      smoke_new_actors_json="$(jq -c \
        --arg namespace "${namespace}" \
        --arg desiredTemplate "${actor_template_name}" \
        --argjson expected "${expected_actor_ids_json}" '
          [.data.actors[]
           | select(.atespace == $namespace)
           | select(.atespace != "ate-golden")
           | select(.actorTemplateNamespace == $namespace)
           | .actorId as $actorId
           | select($actorId | type == "string" and length > 0)
           | select(($expected | index($actorId)) != null)
           | {actorId: $actorId, actorTemplateName: .actorTemplateName}]
          | unique_by(.actorId)
        ' <<<"${substrate_status_after_smoke}")"
      smoke_new_actor_count="$(jq 'length' <<<"${smoke_new_actors_json}")"
      smoke_wrong_template_count="$(jq \
        --arg desiredTemplate "${actor_template_name}" \
        '[.[] | select(.actorTemplateName != $desiredTemplate)] | length' \
        <<<"${smoke_new_actors_json}")"
      if ((smoke_new_actor_count == smoke_expected_actor_count &&
           smoke_wrong_template_count == 0)); then
        smoke_actor_count="${smoke_new_actor_count}"
        smoke_attested=true
        smoke_attestation_source="controller-inventory"
        break
      fi
    elif [[ "${inventory_complete_before_smoke}" != true ]]; then
      exact_actor_records='[]'
      exact_actor_lookup_complete=true
      while IFS= read -r expected_actor_id; do
        exact_actor_json="$(fetch_substrate_actor_by_id "${expected_actor_id}" 2>/dev/null || true)"
        if ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"${exact_actor_json}"; then
          exact_actor_lookup_complete=false
          break
        fi
        exact_actor_records="$(jq -c \
          --argjson actor "${exact_actor_json}" '. + [$actor]' \
          <<<"${exact_actor_records}")"
      done < <(jq -r '.[]' <<<"${expected_actor_ids_json}")

      if [[ "${exact_actor_lookup_complete}" == true ]] && \
         python3 - "${namespace}" "${actor_template_name}" \
           "${smoke_started_epoch}" "${expected_actor_ids_json}" \
           "${exact_actor_records}" <<'PY'
from datetime import datetime, timezone
import json
import sys

namespace, template, started_epoch, expected_raw, records_raw = sys.argv[1:]
expected = json.loads(expected_raw)
records = json.loads(records_raw)
if len(records) != len(expected):
    raise SystemExit(1)

observed = []
for record in records:
    metadata = record.get("metadata") or {}
    actor_id = metadata.get("name")
    created_at = metadata.get("createTime")
    if (
        metadata.get("atespace") != namespace
        or record.get("actorTemplateNamespace") != namespace
        or record.get("actorTemplateName") != template
        or not isinstance(actor_id, str)
        or not isinstance(created_at, str)
    ):
        raise SystemExit(1)
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(1)
    if created.tzinfo is None:
        raise SystemExit(1)
    if created.astimezone(timezone.utc).timestamp() < int(started_epoch):
        raise SystemExit(1)
    observed.append(actor_id)

if len(observed) != len(set(observed)) or sorted(observed) != sorted(expected):
    raise SystemExit(1)
PY
      then
        smoke_actor_count="${smoke_expected_actor_count}"
        smoke_wrong_template_count=0
        smoke_attested=true
        smoke_attestation_source="exact-valkey-key"
        break
      fi
    fi
    sleep "${poll_seconds}"
  done

  if [[ "${smoke_attested}" != true ]]; then
    printf 'Fresh smoke actor attestation failed: observed=%s expected=%s wrongTemplate=%s desiredTemplate=%s.\n' \
      "${smoke_new_actor_count}" "${smoke_expected_actor_count}" "${smoke_wrong_template_count}" \
      "${actor_template_name}" >&2
    exit 1
  fi

  post_smoke_sandbox_generation="$(kubectl -n "${namespace}" get \
    "sandboxagents.kagent.dev/${sandbox_agent}" -o jsonpath='{.metadata.generation}')"
  if [[ "${post_smoke_sandbox_generation}" != "${sandbox_generation}" ]]; then
    printf 'SandboxAgent generation changed during smoke validation (%s -> %s); retry the gate.\n' \
      "${sandbox_generation}" "${post_smoke_sandbox_generation}" >&2
    exit 1
  fi
fi

if ((${#retire_templates[@]} > 0)); then
  substrate_status_json="$(fetch_substrate_status)" || die "failed to query kagent substrate status API"
  substrate_status_is_complete "${substrate_status_json}" || \
    die "substrate status API did not return a complete actor inventory"

  for retire_template in "${retire_templates[@]}"; do
    bound_actor_count="$(jq \
      --arg namespace "${namespace}" \
      --arg template "${retire_template}" '
        [.data.actors[]
         | select(.atespace != "ate-golden")
         | select(.actorTemplateNamespace == $namespace)
         | select(.actorTemplateName == $template)]
        | length
      ' <<<"${substrate_status_json}")"
    if ((bound_actor_count > 0)); then
      printf 'Retirement blocked: %s actor(s) still use ActorTemplate %s.\n' \
        "${bound_actor_count}" "${retire_template}" >&2
      exit 1
    fi
    retirement_templates_json="$(jq -c --arg template "${retire_template}" \
      '. + [$template]' <<<"${retirement_templates_json}")"
  done
  retirement_checked=true
fi

checked_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
jq -n \
  --arg checkedAt "${checked_at}" \
  --arg namespace "${namespace}" \
  --arg revision "${expected_revision}" \
  --arg rmsName "${remote_mcp_server}" \
  --arg rmsUid "${rms_uid}" \
  --argjson rmsGeneration "${rms_generation}" \
  --argjson rmsObservedGeneration "${rms_observed_generation}" \
  --arg sandboxName "${sandbox_agent}" \
  --arg sandboxUid "${sandbox_uid}" \
  --argjson sandboxGeneration "${sandbox_generation}" \
  --argjson sandboxObservedGeneration "${sandbox_observed_generation}" \
  --arg actorTemplateName "${actor_template_name}" \
  --arg actorTemplateUid "${actor_template_uid}" \
  --arg actorTemplateHash "${actor_template_hash}" \
  --arg actorTemplateCreatedAt "${actor_template_created_at}" \
  --arg goldenActorId "${golden_actor_id}" \
  --arg secretName "${expected_secret}" \
  --arg secretUid "${secret_uid}" \
  --arg secretResourceVersion "${secret_resource_version}" \
  --arg workloadName "${workload_name}" \
  --arg workloadUid "${workload_uid}" \
  --argjson workloadGeneration "${workload_generation}" \
  --arg freshSessionId "${fresh_session_id}" \
  --argjson smokeExecuted "${smoke_executed}" \
  --argjson smokeAttested "${smoke_attested}" \
  --argjson smokeActorCount "${smoke_actor_count}" \
  --argjson smokeActorIds "${expected_actor_ids_json:-[]}" \
  --arg smokeAttestationSource "${smoke_attestation_source}" \
  --argjson retirementChecked "${retirement_checked}" \
  --argjson retiredTemplates "${retirement_templates_json}" \
  '{
    checkedAt: $checkedAt,
    namespace: $namespace,
    expectedRevision: $revision,
    remoteMcpServer: {
      name: $rmsName,
      uid: $rmsUid,
      generation: $rmsGeneration,
      observedGeneration: $rmsObservedGeneration,
      accepted: true
    },
    sandboxAgent: {
      name: $sandboxName,
      uid: $sandboxUid,
      generation: $sandboxGeneration,
      observedGeneration: $sandboxObservedGeneration,
      accepted: true
    },
    actorTemplate: {
      name: $actorTemplateName,
      uid: $actorTemplateUid,
      desiredGeneration: ($sandboxGeneration | tostring),
      shapeHash: $actorTemplateHash,
      goldenActorId: (if $goldenActorId == "" then null else $goldenActorId end),
      phase: "Ready",
      createdAt: $actorTemplateCreatedAt
    },
    secret: {
      name: (if $secretName == "" then null else $secretName end),
      uid: (if $secretUid == "" then null else $secretUid end),
      resourceVersion: (if $secretResourceVersion == "" then null else $secretResourceVersion end)
    },
    workload: {
      name: (if $workloadName == "" then null else $workloadName end),
      uid: (if $workloadUid == "" then null else $workloadUid end),
      generation: (if $workloadName == "" then null else $workloadGeneration end)
    },
    smoke: {
      executed: $smokeExecuted,
      actorTemplateAttested: $smokeAttested,
      attestationSource: (if $smokeAttestationSource == "" then null else $smokeAttestationSource end),
      newActorCount: $smokeActorCount,
      actorIds: $smokeActorIds,
      freshSessionId: (if $freshSessionId == "" then null else $freshSessionId end)
    },
    retirement: {
      checked: $retirementChecked,
      drainedActorTemplates: $retiredTemplates
    }
  }'
