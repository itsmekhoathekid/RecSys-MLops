#!/usr/bin/env bash
# Internal deployment runtime. The stable entrypoint only validates arguments
# and dispatches here so callers never depend on implementation layout.
set -euo pipefail

unit_name="${1:?deploy unit is required}"
plan_path="${2:-.ci-release-plan.json}"
[[ -f "${plan_path}" ]] || {
  printf 'release plan does not exist: %s\n' "${plan_path}" >&2
  exit 2
}

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/config.sh
source jenkins/scripts/lib/helm.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/lib/registry.sh
source jenkins/scripts/deploy/preflight/gcp.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/deploy/feast.sh
source jenkins/scripts/deploy/ml_platform.sh
source jenkins/scripts/deploy/serving.sh
source jenkins/scripts/deploy/rollout.sh
source jenkins/scripts/deploy/demo.sh
source jenkins/scripts/deploy/analytics.sh
source jenkins/scripts/deploy/rag.sh
source jenkins/scripts/deploy/datahub.sh
source jenkins/scripts/deploy/agentic.sh

image_registry="${IMAGE_PULL_REGISTRY:-${IMAGE_REGISTRY:-$(python3 jenkins/python/configuration.py gcp imageRegistry)}}"
image_registry="${image_registry%/}"
image_tag="${IMAGE_TAG:-${GIT_COMMIT:-$(git rev-parse HEAD)}}"
namespace_data="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
namespace_kubeflow="${KUBEFLOW_NAMESPACE:-kubeflow}"
namespace_mlops="${MLOPS_NAMESPACE:-experiment-tracking}"
namespace_analytics="${ANALYTICS_NAMESPACE:-analytics}"
namespace_demo="${DEMO_WEB_NAMESPACE:-api-serving}"
promotion_manifest_uri="${PROMOTION_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/latest.json}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
kfp_port_forward_pids=()
kfp_upload_endpoint_result=""
local_model_store_endpoint_result=""
sensitive_helm_values_files=()
cleanup_release_runtime() {
  stop_runtime_port_forwards
  agentic_registry_close_tunnel
  local sensitive_file
  for sensitive_file in "${sensitive_helm_values_files[@]}"; do
    [[ -n "${sensitive_file}" ]] && rm -f -- "${sensitive_file}"
  done
}
trap cleanup_release_runtime EXIT

unit_kind=""
unit_release=""
unit_namespace=""
unit_chart=""
unit_chart_reference=""
unit_action=""
unit_registry_artifact=""
unit_registry_image=""
unit_registry_ref=""
unit_registry_version=""
unit_registry_contract=""
unit_image_names=()
unit_image_paths=()
unit_image_fallback_paths=()
selected_components=","
while IFS=$'\t' read -r record_type value_a value_b value_c value_d; do
  case "${record_type}" in
    UNIT)
      unit_kind="${value_a}"
      unit_release="${value_b}"
      unit_namespace="${value_c}"
      unit_chart="${value_d}"
      ;;
    IMAGE)
      unit_image_names+=("${value_a}")
      unit_image_paths+=("${value_b}")
      unit_image_fallback_paths+=("${value_c}")
      ;;
    ACTION)
      unit_action="${value_a}"
      ;;
    REGISTRY_ARTIFACT)
      unit_registry_artifact="${value_a}"
      ;;
    SELECTED_COMPONENT)
      selected_components+="${value_a},"
      ;;
    *)
      recsys_error "unsupported deploy context record: ${record_type}"
      exit 2
      ;;
  esac
done < <(
  python3 jenkins/python/release_plan.py deploy-context "${unit_name}" --plan "${plan_path}"
)
[[ -n "${unit_kind}" && -n "${unit_release}" && -n "${unit_namespace}" ]] || {
  recsys_error "deploy context is incomplete for ${unit_name}"
  exit 2
}
unit_chart_reference="${unit_chart}"

has_selected_component() {
  [[ "${selected_components}" == *",$1,"* ]]
}

if [[ "${DEPLOY_TARGET:-gcp-production}" == "gcp-production" ]]; then
  [[ -s .ci-deploy/preflight-commit ]] \
    && [[ "$(<.ci-deploy/preflight-commit)" == "$(git rev-parse HEAD)" ]] || {
      recsys_error "production release preflight is missing or stale"
      exit 2
    }
  verify_gcp_deploy_unit \
    "${unit_name}" "${unit_kind}" "${unit_namespace}" "${unit_release}"
fi

read_current_helm_value() {
  local value_path="$1"
  helm get values "${unit_release}" -n "${unit_namespace}" -o json 2>/dev/null \
    | python3 -c '
import json, sys
value = json.load(sys.stdin)
for token in sys.argv[1].split("."):
    value = value.get(token, {}) if isinstance(value, dict) else {}
print(value if isinstance(value, str) else "")
' "${value_path}" 2>/dev/null || true
}

load_agent_registry_lock_context() {
  local record_type value_a value_b
  [[ -n "${unit_registry_artifact}" && "${unit_kind}" == "helm" ]] || return 0
  [[ -s .ci-deploy/agent-registry-lock.json ]] || {
    recsys_error "Agent Registry deployment lock is required for ${unit_name}"
    return 2
  }
  while IFS=$'\t' read -r record_type value_a value_b; do
    case "${record_type}" in
      CHART) unit_chart_reference="${value_a}" ;;
      IMAGE) unit_registry_image="${value_a}" ;;
      ANNOTATION)
        case "${value_a}" in
          recsys.dev/agent-registry-ref) unit_registry_ref="${value_b}" ;;
          recsys.dev/agent-release-version) unit_registry_version="${value_b}" ;;
          recsys.dev/contract-sha256) unit_registry_contract="${value_b}" ;;
        esac
        ;;
      *)
        recsys_error "unsupported Agent Registry lock context: ${record_type}"
        return 2
        ;;
    esac
  done < <(
    python3 -m jenkins.python.agent_registry_release lock-context \
      --plan "${plan_path}" \
      --lock .ci-deploy/agent-registry-lock.json \
      --unit "${unit_name}"
  )
  [[ "${unit_chart_reference}" =~ @sha256:[0-9a-f]{64}$ \
    && -n "${unit_registry_ref}" \
    && -n "${unit_registry_version}" \
    && "${unit_registry_contract}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    recsys_error "Agent Registry deployment lock is incomplete for ${unit_name}"
    return 2
  }
}

resolve_unit_image() {
  local image_name="$1"
  local value_path="$2"
  local fallback_paths="${3:-}"
  local fallback_path
  local reference
  local image_policy
  local -a candidate_fallback_paths=()
  if [[ -n "${unit_registry_artifact}" ]]; then
    [[ "${unit_registry_image}" == "${image_registry}/${image_name}@sha256:"* ]] || {
      recsys_error "Agent Registry lock has no immutable ${image_name} image"
      return 2
    }
    printf '%s' "${unit_registry_image}"
    return 0
  fi
  image_policy="$(mcp_auth_image_policy "${unit_name}")" || return
  if [[ "${image_policy}" == "installed-digest" ]]; then
    reference="$(read_current_helm_value "${value_path}")"
    if [[ -n "${reference}" ]]; then
      if [[ "${DEPLOY_TARGET:-gcp-production}" == "gcp-production" &&
            "${reference}" != *@sha256:* ]]; then
        recsys_error "MCP auth rotation requires the installed ${unit_name} image to be digest-pinned"
        return 2
      fi
      printf '%s' "${reference}"
      return 0
    fi
    # A genuinely new cluster has no installed release to preserve. In that
    # case only, continue with the normal signed release artifact resolution.
  fi
  reference="$(image_manifest_lookup "${image_name}")"
  if [[ -z "${reference}" ]]; then
    reference="$(read_current_helm_value "${value_path}")"
  fi
  if [[ -z "${reference}" ]]; then
    IFS=',' read -r -a candidate_fallback_paths <<<"${fallback_paths}"
    for fallback_path in "${candidate_fallback_paths[@]}"; do
      [[ -n "${fallback_path}" ]] || continue
      reference="$(read_current_helm_value "${fallback_path}")"
      [[ -z "${reference}" ]] || break
    done
  fi
  if [[ -z "${reference}" ]]; then
    if [[ "${DEPLOY_TARGET:-gcp-production}" == "gcp-production" ]]; then
      registry_resolve_latest_digest_reference "${image_name}" "${image_registry}"
      return
    fi
    reference="${image_registry}/${image_name}:${image_tag}"
  fi
  if [[ "${DEPLOY_TARGET:-gcp-production}" == "gcp-production" && "${reference}" != *@sha256:* ]]; then
    registry_resolve_digest_reference "${reference}" "${image_registry}"
  else
    printf '%s' "${reference}"
  fi
}

deploy_helm_unit() {
  local values_file="${unit_chart}/values-gcp.yaml"
  local image_index image_reference
  local helm_args=()
  local helm_failure_args=(--atomic --cleanup-on-fail)
  local deployed_revision_count=0
  local sensitive_values_file=""
  [[ -n "${unit_chart}" ]] || {
    recsys_error "Helm deploy unit ${unit_name} has no chart"
    return 2
  }
  [[ -f "${values_file}" ]] && helm_args+=(-f "${values_file}")
  if [[ -n "${unit_registry_artifact}" ]]; then
    helm_args+=(
      --set-string "releaseMetadata.registryRef=${unit_registry_ref}"
      --set-string "releaseMetadata.version=${unit_registry_version}"
      --set-string "releaseMetadata.contractSha256=${unit_registry_contract}"
    )
  fi
  if mcp_auth_chart_consumes_manifest "${unit_name}"; then
    # The checked-in, non-secret rotation manifest is deliberately supplied
    # on every deploy. This keeps Helm value resets safe across prepare,
    # cutover, rollback, and retirement commits.
    mcp_auth_validate
    helm_args+=(-f "$(mcp_auth_versions_file)")
  fi
  if [[ "${unit_name}" == "online-feature-api" ]]; then
    # The first split-service release adopts the existing Feature API objects
    # after the legacy recsys-serving revision marks them as keep. Helm 4 keeps
    # this flag safe and idempotent for later upgrades of the same release.
    helm_args+=(--take-ownership)

    # An atomic first install uninstalls resources that Helm has just adopted
    # if any later object fails admission. Keep the initial ownership transfer
    # non-destructive; maxUnavailable=0 preserves the serving pod, and every
    # subsequent upgrade returns to atomic rollback semantics.
    deployed_revision_count="$(
      helm history "${unit_release}" -n "${unit_namespace}" -o json 2>/dev/null \
        | python3 -c 'import json, sys; payload = sys.stdin.read(); revisions = json.loads(payload) if payload else []; print(sum(item.get("status") == "deployed" for item in revisions))'
    )" || deployed_revision_count=0
    if [[ "${deployed_revision_count}" == "0" ]] \
      && kubectl -n "${unit_namespace}" get deployment "${unit_release}" >/dev/null 2>&1; then
      helm_failure_args=()
      recsys_log DEPLOY "using non-destructive initial ownership transfer for ${unit_release}"
    fi

    # The registry credential is canonical in recsys-data-platform-secret.
    # Materialize it into a mode-0600 values file so --reset-values cannot
    # restore the chart's development default and the secret never appears in
    # the process arguments or Jenkins console output.
    sensitive_values_file="$(mktemp)"
    sensitive_helm_values_files+=("${sensitive_values_file}")
    chmod 600 "${sensitive_values_file}"
    python3 - "${namespace_data}" "${sensitive_values_file}" <<'PY'
import base64
import json
import subprocess
import sys

namespace, output_path = sys.argv[1:]
payload = json.loads(
    subprocess.check_output(
        ["kubectl", "-n", namespace, "get", "secret", "recsys-data-platform-secret", "-o", "json"],
        text=True,
    )
)
data = payload.get("data", {})
try:
    username = base64.b64decode(data["FEAST_POSTGRES_USER"]).decode("utf-8")
    password = base64.b64decode(data["FEAST_POSTGRES_PASSWORD"]).decode("utf-8")
except (KeyError, UnicodeDecodeError, ValueError) as exc:
    raise SystemExit(f"canonical Feast registry credential is invalid: {exc}") from exc
if not username or not password:
    raise SystemExit("canonical Feast registry credential is empty")

def yaml_scalar(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

with open(output_path, "w", encoding="utf-8") as stream:
    stream.write("config:\n")
    stream.write(f"  feastPostgresUser: {yaml_scalar(username)}\n")
    stream.write(f"  feastPostgresPassword: {yaml_scalar(password)}\n")
PY
    helm_args+=(-f "${sensitive_values_file}")
  fi
  if [[ "${unit_name}" == "milvus" ]]; then
    sensitive_values_file="$(mktemp)"
    sensitive_helm_values_files+=("${sensitive_values_file}")
    chmod 600 "${sensitive_values_file}"
    python3 - "${namespace_data}" "${sensitive_values_file}" <<'PY'
import base64
import json
import subprocess
import sys

namespace, output_path = sys.argv[1:]
payload = json.loads(subprocess.check_output(
    ["kubectl", "-n", namespace, "get", "secret", "recsys-data-platform-secret", "-o", "json"],
    text=True,
))
data = payload.get("data", {})
access = base64.b64decode(data["AWS_ACCESS_KEY_ID"]).decode("utf-8")
secret = base64.b64decode(data["AWS_SECRET_ACCESS_KEY"]).decode("utf-8")

def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"

with open(output_path, "w", encoding="utf-8") as stream:
    stream.write("milvus:\n  externalS3:\n")
    stream.write(f"    accessKey: {quote(access)}\n")
    stream.write(f"    secretKey: {quote(secret)}\n")
PY
    helm_args+=(-f "${sensitive_values_file}")
  fi
  for image_index in "${!unit_image_names[@]}"; do
    image_reference="$(resolve_unit_image \
      "${unit_image_names[image_index]}" \
      "${unit_image_paths[image_index]}" \
      "${unit_image_fallback_paths[image_index]}")"
    helm_args+=(--set-string "${unit_image_paths[image_index]}=${image_reference}")
  done
  if [[ "${unit_name}" == "data-config" && -s .ci-deploy/kfp-upload.json ]]; then
    local kfp_pipeline_name kfp_pipeline_version
    IFS=$'\t' read -r kfp_pipeline_name kfp_pipeline_version < <(
      python3 -c '
import json
payload = json.load(open(".ci-deploy/kfp-upload.json", encoding="utf-8"))
print("{}\t{}".format(payload["pipeline_name"], payload.get("pipeline_version_id", "")))
'
    )
    helm_args+=(
      --set "observability.kfpPipelineName=${kfp_pipeline_name}"
      --set-string "observability.kfpPipelineVersionId=${kfp_pipeline_version}"
    )
  elif [[ "${unit_name}" == "data-config" ]]; then
    local current_kfp_name current_kfp_version
    current_kfp_name="$(read_current_helm_value observability.kfpPipelineName)"
    current_kfp_version="$(read_current_helm_value observability.kfpPipelineVersionId)"
    [[ -n "${current_kfp_name}" ]] \
      && helm_args+=(--set "observability.kfpPipelineName=${current_kfp_name}")
    [[ -n "${current_kfp_version}" ]] \
      && helm_args+=(--set-string "observability.kfpPipelineVersionId=${current_kfp_version}")
  fi
  helm upgrade --install "${unit_release}" "${unit_chart_reference}" \
    --namespace "${unit_namespace}" \
    --create-namespace \
    --reset-values \
    "${helm_failure_args[@]}" \
    --wait \
    --wait-for-jobs \
    --history-max "${HELM_HISTORY_MAX:-10}" \
    --timeout "${timeout}" \
    "${helm_args[@]}"
  if [[ -n "${sensitive_values_file}" ]]; then
    rm -f -- "${sensitive_values_file}"
  fi
}

deploy_agentic_helm_unit() {
  local preflight_function="$1"
  local include_agent="${2:-false}"
  "${preflight_function}" "${include_agent}"
  deploy_helm_unit
}

deploy_agent_with_cutover_probes() {
  local preflight_function="$1"
  local service="$2"
  local phase old_revision new_revision duration old_log new_log
  local old_pid new_pid deploy_status=0 probe_status=0
  phase="$(mcp_auth_transition_phase "${service}")"
  if [[ "${phase}" != "cutover-or-rollback" ]]; then
    deploy_agentic_helm_unit "${preflight_function}" true
    return
  fi

  old_revision="$(mcp_auth_previous_get "${service}" activeRevision)"
  new_revision="$(mcp_auth_active_revision "${service}")"
  [[ "${old_revision}" != "${new_revision}" ]] || {
    recsys_error "cutover did not change ${service} activeRevision"
    return 2
  }
  duration="${MCP_AUTH_CUTOVER_PROBE_SECONDS:-${timeout%s}}"
  [[ "${duration}" =~ ^[1-9][0-9]*$ ]] || {
    recsys_error "MCP_AUTH_CUTOVER_PROBE_SECONDS must be a positive integer"
    return 2
  }
  mkdir -p reports/agentic
  old_log="reports/agentic/mcp-auth-cutover-${service}-${old_revision}.jsonl"
  new_log="reports/agentic/mcp-auth-cutover-${service}-${new_revision}.jsonl"
  bash ops/validation/mcp_auth_continuous_probe.sh \
    "${service}" "${old_revision}" "${duration}" >"${old_log}" 2>&1 &
  old_pid=$!
  bash ops/validation/mcp_auth_continuous_probe.sh \
    "${service}" "${new_revision}" "${duration}" >"${new_log}" 2>&1 &
  new_pid=$!

  deploy_agentic_helm_unit "${preflight_function}" true || deploy_status=$?
  if ((deploy_status != 0)); then
    kill "${old_pid}" "${new_pid}" >/dev/null 2>&1 || true
    wait "${old_pid}" >/dev/null 2>&1 || true
    wait "${new_pid}" >/dev/null 2>&1 || true
    return "${deploy_status}"
  fi
  wait "${old_pid}" || probe_status=$?
  wait "${new_pid}" || probe_status=$?
  ((probe_status == 0)) || {
    recsys_error "continuous MCP auth probe failed during ${service} cutover"
    return "${probe_status}"
  }
}

verify_mcp_retirement_before_deploy() {
  local service="$1"
  if [[ "$(mcp_auth_transition_phase "${service}")" == "retire" ]]; then
    bash ops/validation/mcp_auth_retirement_gate.sh "${service}"
  fi
}

deploy_unit_feature_rag_mcp() {
  verify_mcp_retirement_before_deploy featureRag
  deploy_agentic_helm_unit agentic_preflight false
  mcp_auth_verify_prepare featureRag agentic_mcp_protocol_smoke
}
deploy_unit_context_agent() {
  deploy_agent_with_cutover_probes agentic_preflight featureRag
  MCP_AUTH_GATE_EVIDENCE="reports/agentic/mcp-auth-context-rollout-gate.json" \
    mcp_auth_rollout_gate featureRag metadata-only
}
deploy_unit_recommendation_mcp() {
  verify_mcp_retirement_before_deploy recommendation
  deploy_agentic_helm_unit recommendation_agentic_preflight false
  mcp_auth_verify_prepare recommendation recommendation_mcp_protocol_smoke
}
deploy_unit_recommendation_agent() {
  deploy_agent_with_cutover_probes recommendation_agentic_preflight recommendation
  MCP_AUTH_GATE_EVIDENCE="reports/agentic/mcp-auth-recommendation-rollout-gate.json" \
    mcp_auth_rollout_gate recommendation metadata-only
}
deploy_unit_coordinator_agent() {
  deploy_agentic_helm_unit coordinator_agentic_preflight false
}

deploy_unit_feature_registry() { feast_registry_apply "$(resolve_release_image recsys-feature-store)"; }
deploy_unit_rag_feature_registry() { rag_feature_registry_apply "$(resolve_release_image recsys-rag-admin)"; }
deploy_unit_milvus_credentials() { rag_milvus_credentials_bootstrap "$(resolve_release_image recsys-rag-admin)"; }
deploy_unit_datahub_catalog() { datahub_catalog_sync "$(resolve_release_image recsys-datahub-ops)"; }
deploy_unit_mlflow() { deploy_mlflow; }
deploy_unit_analytics() { deploy_analytics; }
deploy_unit_kserve() { deploy_kserve; }
deploy_unit_rollout() { deploy_rollout_watcher "${unit_namespace}"; }
deploy_unit_demo_web() { deploy_demo_web; }
deploy_unit_kubeflow_bst_package() {
  mkdir -p .ci-deploy
  open_kfp_upload_endpoint
  KFP_UPLOAD_RESULT_PATH=.ci-deploy/kfp-upload.json \
    KFP_ENDPOINT="${kfp_upload_endpoint_result}" \
    bash jenkins/scripts/deploy/upload_kfp_package.sh
}

dispatch_deploy_unit() {
  local handler="deploy_unit_${unit_name//-/_}"
  if [[ -n "${unit_action}" ]]; then
    recsys_error "unsupported deploy action: ${unit_action}"
    return 2
  elif declare -F "${handler}" >/dev/null; then
    "${handler}"
  elif [[ "${unit_kind}" == "helm" ]]; then
    deploy_helm_unit
  else
    recsys_error "unsupported deploy unit: ${unit_name} (${unit_kind})"
    return 2
  fi
}

load_agent_registry_lock_context
dispatch_deploy_unit
