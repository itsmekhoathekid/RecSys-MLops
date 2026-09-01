#!/usr/bin/env bash
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
namespace_api="${API_NAMESPACE:-api-serving}"
namespace_kserve="${KSERVE_NAMESPACE:-kserve-triton-inference}"
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
unit_image_names=()
unit_image_paths=()
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

resolve_unit_image() {
  local image_name="$1"
  local value_path="$2"
  local reference
  reference="$(image_manifest_lookup "${image_name}")"
  if [[ -z "${reference}" ]]; then
    reference="$(read_current_helm_value "${value_path}")"
  fi
  if [[ -z "${reference}" ]]; then
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
      "${unit_image_names[image_index]}" "${unit_image_paths[image_index]}")"
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
  helm upgrade --install "${unit_release}" "${unit_chart}" \
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

case "${unit_name}" in
  data-config|data-lakehouse|source-store|event-stream|feature-store|kafka-connect|streaming|airflow|online-feature-api|inference-api|milvus|rag-api|feature-rag-mcp|context-agent|recommendation-mcp|recommendation-agent|coordinator-agent)
    sandbox_agent_name=""
    sandbox_agent_previous_revision=""
    case "${unit_name}" in
      context-agent)
        sandbox_agent_name="recsys-context-agent-sandbox"
        ;;
      recommendation-agent)
        sandbox_agent_name="recsys-recommendation-agent-sandbox"
        ;;
      coordinator-agent)
        sandbox_agent_name="recsys-coordinator-agent-sandbox"
        ;;
    esac
    if [[ -n "${sandbox_agent_name}" ]]; then
      sandbox_agent_previous_revision="$(
        sandbox_agent_model_revision "${sandbox_agent_name}"
      )"
    fi
    if [[ "${unit_name}" == "feature-rag-mcp" ]]; then
      agentic_preflight false
    elif [[ "${unit_name}" == "context-agent" ]]; then
      agentic_preflight true
    elif [[ "${unit_name}" == "recommendation-mcp" ]]; then
      recommendation_agentic_preflight false
    elif [[ "${unit_name}" == "recommendation-agent" ]]; then
      recommendation_agentic_preflight true
    elif [[ "${unit_name}" == "coordinator-agent" ]]; then
      coordinator_agentic_preflight false
    fi
    deploy_helm_unit
    if [[ -n "${sandbox_agent_name}" ]]; then
      sandbox_agent_rebuild_golden_if_revision_changed \
        "${sandbox_agent_name}" "${sandbox_agent_previous_revision}"
    fi
    ;;
  feature-registry)
    feast_registry_apply "$(resolve_release_image recsys-feature-store)"
    ;;
  rag-feature-registry)
    rag_feature_registry_apply "$(resolve_release_image recsys-data-ingestion)"
    ;;
  milvus-credentials)
    rag_milvus_credentials_bootstrap "$(resolve_release_image recsys-data-ingestion)"
    ;;
  datahub-catalog)
    datahub_catalog_sync "$(resolve_release_image recsys-data-ingestion)"
    ;;
  feature-rag-mcp-registry)
    publish_feature_rag_mcp_registry
    ;;
  context-agent-registry)
    publish_context_agent_registry
    ;;
  recommendation-mcp-registry)
    publish_recommendation_mcp_registry
    ;;
  recommendation-agent-registry)
    publish_recommendation_agent_registry
    ;;
  coordinator-agent-registry)
    publish_coordinator_agent_registry
    ;;
  mlflow)
    deploy_mlflow
    ;;
  kubeflow-bst-package)
    mkdir -p .ci-deploy
    open_kfp_upload_endpoint
    KFP_UPLOAD_RESULT_PATH=.ci-deploy/kfp-upload.json \
      KFP_ENDPOINT="${kfp_upload_endpoint_result}" \
      bash jenkins/scripts/deploy/upload_kfp_package.sh
    ;;
  analytics)
    deploy_analytics
    ;;
  kserve)
    deploy_kserve
    ;;
  rollout)
    deploy_rollout_watcher "${unit_namespace}"
    ;;
  demo-web)
    deploy_demo_web
    ;;
  *)
    recsys_error "unsupported deploy unit: ${unit_name} (${unit_kind})"
    exit 2
    ;;
esac
