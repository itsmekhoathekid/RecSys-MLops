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
source jenkins/scripts/lib/kubernetes.sh
source jenkins/scripts/lib/port_forward.sh
source jenkins/scripts/lib/registry.sh
source jenkins/scripts/deploy/preflight/gcp.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/deploy/feast.sh
source jenkins/scripts/deploy/ml_platform.sh
source jenkins/scripts/deploy/serving.sh
source jenkins/scripts/deploy/rollout.sh
source jenkins/scripts/deploy/demo.sh
source jenkins/scripts/deploy/analytics.sh

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
local_model_store_endpoint_result=""
cleanup_release_deploy() {
  cleanup_port_forwards
  port_forward_cleanup
}
trap cleanup_release_deploy EXIT

if [[ "${DEPLOY_TARGET:-gcp-production}" == "gcp-production" ]]; then
  branch_name="${BRANCH_NAME:-${GIT_BRANCH:-}}"
  checked_out_main=0
  if git rev-parse --verify origin/main >/dev/null 2>&1 \
    && [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]]; then
    checked_out_main=1
  fi
  if [[ "${branch_name}" != "main" && "${branch_name}" != "origin/main" ]] \
    && [[ "${checked_out_main}" != "1" ]] \
    && ! recsys_is_true "${FORCE_DEPLOY:-0}"; then
    recsys_error "GCP production deploy requires main or FORCE_DEPLOY=true"
    exit 2
  fi
  recsys_is_true "${PUBLISH_IMAGES:-0}" || {
    recsys_error "GCP production deploy requires PUBLISH_IMAGES=true"
    exit 2
  }
  gcp_production_preflight "${unit_name}" "${plan_path}"
fi

unit_json="$(python3 jenkins/python/release_plan.py unit "${unit_name}")"
unit_kind="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["kind"])' <<<"${unit_json}")"
unit_release="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["release"])' <<<"${unit_json}")"
unit_namespace="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["namespace"])' <<<"${unit_json}")"

current_helm_value() {
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

resolved_image() {
  local image_name="$1"
  local value_path="$2"
  local ref
  ref="$(image_manifest_lookup "${image_name}")"
  if [[ -z "${ref}" ]]; then
    ref="$(current_helm_value "${value_path}")"
  fi
  if [[ -z "${ref}" ]]; then
    ref="${image_registry}/${image_name}:${image_tag}"
  fi
  if [[ "${DEPLOY_TARGET:-gcp-production}" == "gcp-production" && "${ref}" != *@sha256:* ]]; then
    registry_resolve_digest_reference "${ref}" "${image_registry}"
  else
    printf '%s' "${ref}"
  fi
}

deploy_catalog_helm_unit() {
  local chart
  local values_file
  local image_name
  local value_path
  local image_ref
  local helm_args=()
  chart="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["chart"])' <<<"${unit_json}")"
  values_file="${chart}/values-gcp.yaml"
  [[ -f "${values_file}" ]] && helm_args+=(-f "${values_file}")
  while IFS=$'\t' read -r image_name value_path; do
    [[ -n "${image_name}" ]] || continue
    image_ref="$(resolved_image "${image_name}" "${value_path}")"
    helm_args+=(--set-string "${value_path}=${image_ref}")
  done < <(
    python3 -c '
import json, sys
for image_name, value_path in json.load(sys.stdin).get("imageValues", {}).items():
    print("{}\t{}".format(image_name, value_path))
' <<<"${unit_json}"
  )
  if [[ "${unit_name}" == "data-config" && -s .ci-deploy/kfp-upload.json ]]; then
    helm_args+=(
      --set "observability.kfpPipelineName=$(python3 -c 'import json; print(json.load(open(".ci-deploy/kfp-upload.json"))["pipeline_name"])')"
      --set-string "observability.kfpPipelineVersionId=$(python3 -c 'import json; print(json.load(open(".ci-deploy/kfp-upload.json")).get("pipeline_version_id", ""))')"
    )
  fi
  helm upgrade --install "${unit_release}" "${chart}" \
    --namespace "${unit_namespace}" \
    --create-namespace \
    --reset-values \
    --atomic \
    --cleanup-on-fail \
    --wait \
    --wait-for-jobs \
    --history-max "${HELM_HISTORY_MAX:-10}" \
    --timeout "${timeout}" \
    "${helm_args[@]}"
}

case "${unit_name}" in
  data-config|data-lakehouse|source-store|event-stream|feature-store|kafka-connect|streaming|airflow)
    deploy_catalog_helm_unit
    ;;
  feature-registry)
    feast_registry_apply "$(image recsys-feature-store)"
    ;;
  mlflow)
    deploy_mlflow
    ;;
  kubeflow-bst-package)
    mkdir -p .ci-deploy
    KFP_UPLOAD_RESULT_PATH=.ci-deploy/kfp-upload.json \
      KFP_ENDPOINT="$(kfp_endpoint_for_upload)" \
      RECSYS_PIPELINE_IMAGE="$(image recsys-mlops-training)" \
      RECSYS_RAY_IMAGE="$(image recsys-mlops-training)" \
      RECSYS_SPARK_IMAGE="$(image recsys-spark)" \
      bash jenkins/scripts/deploy/kfp_version.sh
    ;;
  analytics)
    deploy_analytics
    ;;
  serving)
    if python3 -c 'import json,sys; raise SystemExit(0 if "api" in json.load(open(sys.argv[1]))["components"] else 1)' "${plan_path}"; then
      deploy_api
    fi
    if python3 -c 'import json,sys; raise SystemExit(0 if "kserve" in json.load(open(sys.argv[1]))["components"] else 1)' "${plan_path}"; then
      deploy_kserve
    fi
    ;;
  rollout)
    deploy_rollout_watcher
    ;;
  demo-web)
    deploy_demo_web
    ;;
  *)
    recsys_error "unsupported deploy unit: ${unit_name} (${unit_kind})"
    exit 2
    ;;
esac
