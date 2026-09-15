#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "$0")/../.."
source jenkins/scripts/deploy/agentic/rotation.sh

service="${1:?usage: $0 <featureRag|recommendation> [evidence-output.json]}"
timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
output="${2:-reports/agentic/mcp-auth-retirement-${service}-${timestamp}.json}"
namespace="${KAGENT_NAMESPACE:-kagent}"
window="${MCP_AUTH_RETIRE_WINDOW:-15m}"
timeout_seconds="${MCP_AUTH_RETIRE_TIMEOUT_SECONDS:-1800}"
poll_seconds="${MCP_AUTH_RETIRE_POLL_SECONDS:-30}"
prometheus_service="${MCP_AUTH_PROMETHEUS_SERVICE:-recsys-prometheus}"
prometheus_namespace="${MCP_AUTH_PROMETHEUS_NAMESPACE:-observability}"
prometheus_port="${MCP_AUTH_PROMETHEUS_LOCAL_PORT:-19090}"
port_forward_pid=""
port_forward_log=""
gate_file=""

cleanup() {
  if [[ -n "${port_forward_pid}" ]]; then
    kill "${port_forward_pid}" >/dev/null 2>&1 || true
    wait "${port_forward_pid}" >/dev/null 2>&1 || true
  fi
  [[ -z "${port_forward_log}" ]] || rm -f -- "${port_forward_log}"
  [[ -z "${gate_file}" ]] || rm -f -- "${gate_file}"
}
trap cleanup EXIT

[[ "${MCP_AUTH_RETIRE_APPROVED:-}" == "yes" ]] || {
  printf 'Set MCP_AUTH_RETIRE_APPROVED=yes only after reviewing the retirement change.\n' >&2
  exit 2
}
[[ "${window}" =~ ^[1-9][0-9]*[smhd]$ ]] || {
  printf 'MCP_AUTH_RETIRE_WINDOW must be a positive Prometheus duration.\n' >&2
  exit 2
}
[[ "${timeout_seconds}" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "${poll_seconds}" =~ ^[1-9][0-9]*$ ]] || exit 2
window_amount="${window%?}"
case "${window: -1}" in
  s) window_multiplier=1 ;;
  m) window_multiplier=60 ;;
  h) window_multiplier=3600 ;;
  d) window_multiplier=86400 ;;
esac
window_seconds=$((10#${window_amount} * window_multiplier))
minimum_telemetry_samples="${MCP_AUTH_RETIRE_MIN_TELEMETRY_SAMPLES:-$(((window_seconds + 29) / 30))}"
maximum_telemetry_age_seconds="${MCP_AUTH_RETIRE_MAX_TELEMETRY_AGE_SECONDS:-120}"
[[ "${minimum_telemetry_samples}" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ "${maximum_telemetry_age_seconds}" =~ ^[1-9][0-9]*$ ]] || exit 2
[[ ! -e "${output}" ]] || {
  printf 'Refusing to overwrite retirement evidence: %s\n' "${output}" >&2
  exit 2
}

mcp_auth_validate
phase="$(mcp_auth_transition_phase "${service}")"
[[ "${phase}" == "retire" ]] || {
  printf 'Retirement gate requires a single %s deploy:true -> false transition; got %s.\n' \
    "${service}" "${phase}" >&2
  exit 2
}
retiring_revision="$(mcp_auth_changed_revision "${service}" retire)"
retiring_workload="$(mcp_auth_get_revision_field \
  "${service}" "${retiring_revision}" workloadName)"
retiring_secret="$(mcp_auth_get_revision_field \
  "${service}" "${retiring_revision}" secretName)"
active_revision="$(mcp_auth_active_revision "${service}")"
active_workload="$(mcp_auth_active_workload "${service}")"

case "${service}" in
  featureRag)
    remote_mcp_server=recsys-feature-rag-mcp
    sandbox_agent=recsys-context-agent-sandbox
    ;;
  recommendation)
    remote_mcp_server=recsys-recommendation-mcp
    sandbox_agent=recsys-recommendation-agent-sandbox
    ;;
  *)
    printf 'unsupported MCP auth service: %s\n' "${service}" >&2
    exit 2
    ;;
esac

kubectl -n "${namespace}" rollout status "deployment/${retiring_workload}" \
  --timeout="${MCP_AUTH_RETIRE_READY_TIMEOUT:-600s}"
kubectl -n "${namespace}" rollout status "deployment/${active_workload}" \
  --timeout="${MCP_AUTH_RETIRE_READY_TIMEOUT:-600s}"

sandbox_generation="$(kubectl -n "${namespace}" get \
  "sandboxagents.kagent.dev/${sandbox_agent}" -o jsonpath='{.metadata.generation}')"
templates_json="$(kubectl -n "${namespace}" get actortemplates.ate.dev \
  -l "kagent.dev/sandbox-agent=${sandbox_agent}" -o json)"
current_template="$(jq -r --arg generation "${sandbox_generation}" '
  [.items[]
   | select(.metadata.deletionTimestamp == null)
   | select(.metadata.annotations["kagent.dev/desired-generation"] == $generation)
   | select(.status.phase == "Ready")]
  | sort_by(.metadata.creationTimestamp)
  | last.metadata.name // empty
' <<<"${templates_json}")"
[[ -n "${current_template}" ]] || {
  printf 'No Ready ActorTemplate matches current SandboxAgent generation %s.\n' \
    "${sandbox_generation}" >&2
  exit 1
}
retired_templates=()
while IFS= read -r template; do
  [[ -z "${template}" ]] || retired_templates+=("${template}")
done < <(jq -r --arg current "${current_template}" '
  [.items[]
   | select(.metadata.deletionTimestamp == null)
   | .metadata.name
   | select(. != $current)]
  | sort[]
' <<<"${templates_json}")
if ((${#retired_templates[@]} == 0)); then
  printf 'No prior ActorTemplate exists; retirement drain cannot be proven.\n' >&2
  exit 1
fi

port_forward_log="$(mktemp "${TMPDIR:-/tmp}/mcp-auth-retire-prometheus.XXXXXX")"
kubectl -n "${prometheus_namespace}" port-forward \
  "service/${prometheus_service}" "${prometheus_port}:9090" \
  >"${port_forward_log}" 2>&1 &
port_forward_pid=$!
prometheus_url="http://127.0.0.1:${prometheus_port}/api/v1/query"
prometheus_ready=false
for ((attempt = 0; attempt < 30; attempt++)); do
  if ! kill -0 "${port_forward_pid}" >/dev/null 2>&1; then
    sed -n '1,80p' "${port_forward_log}" >&2
    printf 'Prometheus port-forward exited early.\n' >&2
    exit 1
  fi
  if curl -fsS --get --max-time 10 \
    --data-urlencode 'query=vector(1)' "${prometheus_url}" >/dev/null 2>&1; then
    prometheus_ready=true
    break
  fi
  sleep 1
done
[[ "${prometheus_ready}" == true ]] || {
  printf 'Timed out waiting for Prometheus port-forward.\n' >&2
  exit 1
}

# Prometheus scrapes each MCP /metrics endpoint every 15s through the Istio
# sidecar. Those monitoring requests prove telemetry liveness below, but are
# not old-slot application traffic and would otherwise keep retirement above
# zero forever.
old_traffic_query="sum(increase(istio_requests_total{reporter=\"destination\",destination_workload_namespace=\"${namespace}\",destination_workload=\"${retiring_workload}\",source_workload!=\"recsys-prometheus\",source_workload_namespace!=\"observability\"}[${window}])) or vector(0)"
auth_error_query="sum(increase(istio_requests_total{reporter=\"destination\",destination_workload_namespace=\"${namespace}\",destination_workload=~\"${retiring_workload}|${active_workload}\",source_workload!=\"recsys-prometheus\",source_workload_namespace!=\"observability\",response_code=~\"401|5..\"}[${window}])) or vector(0)"
telemetry_selector="istio_requests_total{reporter=\"destination\",destination_workload_namespace=\"${namespace}\",destination_workload=~\"${retiring_workload}|${active_workload}\"}"
telemetry_current_query="count(count by (destination_workload) (${telemetry_selector}))"
telemetry_start_query="count(count by (destination_workload) (${telemetry_selector} offset ${window}))"
telemetry_samples_query="min(max by (destination_workload) (count_over_time(${telemetry_selector}[${window}])))"
telemetry_age_query="max(time() - max by (destination_workload) (timestamp(${telemetry_selector})))"

query_value() {
  local query="$1"
  curl -fsS --get --max-time 15 --data-urlencode "query=${query}" \
    "${prometheus_url}" | jq -er '
      select(.status == "success")
      | .data.result
      | select(length == 1)
      | .[0].value[1]
      | tonumber
    '
}

read_window_metrics() {
  old_traffic="$(query_value "${old_traffic_query}" 2>/dev/null || printf 'invalid')"
  auth_errors="$(query_value "${auth_error_query}" 2>/dev/null || printf 'invalid')"
  telemetry_current_workloads="$(query_value "${telemetry_current_query}" 2>/dev/null || printf 'invalid')"
  telemetry_start_workloads="$(query_value "${telemetry_start_query}" 2>/dev/null || printf 'invalid')"
  telemetry_samples="$(query_value "${telemetry_samples_query}" 2>/dev/null || printf 'invalid')"
  telemetry_age_seconds="$(query_value "${telemetry_age_query}" 2>/dev/null || printf 'invalid')"
}

window_is_clean() {
  [[ "${old_traffic}" != "invalid" &&
     "${auth_errors}" != "invalid" &&
     "${telemetry_current_workloads}" != "invalid" &&
     "${telemetry_start_workloads}" != "invalid" &&
     "${telemetry_samples}" != "invalid" &&
     "${telemetry_age_seconds}" != "invalid" ]] || return 1
  jq -en \
    --argjson oldTraffic "${old_traffic}" \
    --argjson authErrors "${auth_errors}" \
    --argjson currentWorkloads "${telemetry_current_workloads}" \
    --argjson startWorkloads "${telemetry_start_workloads}" \
    --argjson samples "${telemetry_samples}" \
    --argjson minimumSamples "${minimum_telemetry_samples}" \
    --argjson ageSeconds "${telemetry_age_seconds}" \
    --argjson maximumAge "${maximum_telemetry_age_seconds}" '
      $oldTraffic == 0 and
      $authErrors == 0 and
      $currentWorkloads == 2 and
      $startWorkloads == 2 and
      $samples >= $minimumSamples and
      $ageSeconds <= $maximumAge
    ' >/dev/null
}

wait_for_clean_window() {
  local deadline
  deadline=$((SECONDS + timeout_seconds))
  while ((SECONDS < deadline)); do
    read_window_metrics
    if window_is_clean; then
      return 0
    fi
    printf 'Waiting for clean %s window: old-slot requests=%s auth/5xx=%s telemetry(current/start/samples/age)=%s/%s/%s/%ss\n' \
      "${window}" "${old_traffic}" "${auth_errors}" \
      "${telemetry_current_workloads}" "${telemetry_start_workloads}" \
      "${telemetry_samples}" "${telemetry_age_seconds}" >&2
    sleep "${poll_seconds}"
  done
  return 1
}

wait_for_clean_window || {
  printf 'Retirement window did not become clean before timeout.\n' >&2
  exit 1
}

gate_args=(
  --namespace "${namespace}"
  --remote-mcp-server "${remote_mcp_server}"
  --sandbox-agent "${sandbox_agent}"
  --expected-revision "${active_revision}"
  --expected-url "http://${active_workload}.${namespace}.svc.cluster.local:8080/mcp"
  --expected-secret "$(mcp_auth_active_secret "${service}")"
  --metadata-only
)
for template in "${retired_templates[@]}"; do
  gate_args+=(--retire-template "${template}")
done
gate_file="$(mktemp "${TMPDIR:-/tmp}/mcp-auth-retirement-gate.XXXXXX")"
bash ops/validation/mcp_auth_rotation_gate.sh "${gate_args[@]}" >"${gate_file}"

# Recheck traffic and telemetry after actor inventory validation so the
# evidence is the latest possible pre-delete snapshot.
read_window_metrics
window_is_clean || {
  printf 'Traffic or telemetry changed while validating actors; retry retirement.\n' >&2
  exit 1
}

secret_uid="$(kubectl -n "${namespace}" get "secret/${retiring_secret}" \
  -o jsonpath='{.metadata.uid}')"
secret_resource_version="$(kubectl -n "${namespace}" get "secret/${retiring_secret}" \
  -o jsonpath='{.metadata.resourceVersion}')"
external_secret_uid="$(kubectl -n "${namespace}" get \
  "externalsecret.external-secrets.io/${retiring_secret}" \
  -o jsonpath='{.metadata.uid}')"

mkdir -p "$(dirname "${output}")"
jq -n \
  --arg checkedAt "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --arg service "${service}" \
  --arg revision "${retiring_revision}" \
  --arg workload "${retiring_workload}" \
  --arg secret "${retiring_secret}" \
  --arg secretUid "${secret_uid}" \
  --arg secretResourceVersion "${secret_resource_version}" \
  --arg externalSecretUid "${external_secret_uid}" \
  --arg window "${window}" \
  --argjson telemetryCurrentWorkloads "${telemetry_current_workloads}" \
  --argjson telemetryStartWorkloads "${telemetry_start_workloads}" \
  --argjson telemetrySamples "${telemetry_samples}" \
  --argjson telemetryMinimumSamples "${minimum_telemetry_samples}" \
  --argjson telemetryAgeSeconds "${telemetry_age_seconds}" \
  --argjson telemetryMaximumAgeSeconds "${maximum_telemetry_age_seconds}" \
  --argjson gate "$(<"${gate_file}")" \
  '{
    checkedAt: $checkedAt,
    service: $service,
    retiringRevision: $revision,
    retiringWorkload: $workload,
    retiringSecret: {
      name: $secret,
      uid: $secretUid,
      resourceVersion: $secretResourceVersion,
      externalSecretUid: $externalSecretUid
    },
    traffic: {
      window: $window,
      oldSlotRequests: 0,
      authOr5xxErrors: 0,
      excludedMonitoringSource: "observability/recsys-prometheus",
      telemetry: {
        currentWorkloads: $telemetryCurrentWorkloads,
        windowStartWorkloads: $telemetryStartWorkloads,
        minimumSamplesObserved: $telemetrySamples,
        minimumSamplesRequired: $telemetryMinimumSamples,
        maximumAgeSecondsObserved: $telemetryAgeSeconds,
        maximumAgeSecondsAllowed: $telemetryMaximumAgeSeconds
      }
    },
    actorGate: $gate,
    approved: true
  }' >"${output}"
printf '%s\n' "${output}"
