#!/usr/bin/env bash
set -euo pipefail

component="${1:?component is required}"
if [[ -n "${CI_TMP_ROOT:-}" ]]; then
  ci_profile="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["ciProfile"])' \
      <<<"$(python3 jenkins/python/configuration.py component "${component}")"
  )"
  component_environment="${CI_TMP_ROOT}/envs/${ci_profile}"
  if [[ -x "${component_environment}/bin/python" ]]; then
    export UV_PROJECT_ENVIRONMENT="${component_environment}"
  fi
fi

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/config.sh
source jenkins/scripts/lib/helm.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/lib/kubernetes.sh
source jenkins/scripts/lib/port_forward.sh
source jenkins/scripts/lib/registry.sh
source jenkins/scripts/deploy/preflight/gcp.sh
source jenkins/scripts/deploy/transaction.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/deploy/database.sh
source jenkins/scripts/deploy/feast.sh
source jenkins/scripts/deploy/rollout.sh
source jenkins/scripts/deploy/data_platform.sh
source jenkins/scripts/deploy/ml_platform.sh
source jenkins/scripts/deploy/serving.sh
source jenkins/scripts/deploy/demo.sh
source jenkins/scripts/deploy/analytics.sh
source jenkins/scripts/test/runtime.sh
source jenkins/scripts/test/data_platform.sh
source jenkins/scripts/test/ml_platform.sh
source jenkins/scripts/test/serving.sh
source jenkins/scripts/test/demo.sh
source jenkins/scripts/test/analytics.sh
source jenkins/scripts/test/dispatch.sh
source jenkins/scripts/deploy/dispatch.sh

image_registry="${IMAGE_PULL_REGISTRY:-${IMAGE_REGISTRY:-localhost:5001/recsys}}"
image_registry="${image_registry%/}"
image_tag="${IMAGE_TAG:-${GIT_COMMIT:-}}"
namespace_data="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
namespace_api="${API_NAMESPACE:-api-serving}"
namespace_kserve="${KSERVE_NAMESPACE:-kserve-triton-inference}"
namespace_kubeflow="${KUBEFLOW_NAMESPACE:-kubeflow}"
namespace_mlops="${MLOPS_NAMESPACE:-experiment-tracking}"
namespace_analytics="${ANALYTICS_NAMESPACE:-analytics}"
namespace_demo="${DEMO_WEB_NAMESPACE:-api-serving}"
namespace_ci="${CI_NAMESPACE:-ci}"
promotion_manifest_uri="${PROMOTION_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/latest.json}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
run_node_rebalance="${RUN_NODE_REBALANCE:-1}"
validate_node_rebalance="${VALIDATE_NODE_REBALANCE:-1}"
kfp_port_forward_pids=()
local_model_store_endpoint_result=""

if [[ -n "${JENKINS_HOME:-}" ]]; then
  export UV_CACHE_DIR="${JENKINS_UV_CACHE_DIR:-${JENKINS_HOME}/.cache/uv}"
fi

if [[ -z "${image_tag}" ]]; then
  image_tag="$(git rev-parse --short=12 HEAD)"
fi

trap component_deploy_on_exit EXIT

if recsys_is_true "${RECOVER_ONLY:-0}"; then
  if [[ "${DEPLOY_TARGET:-local}" == "gcp-production" ]]; then
    python3 jenkins/python/configuration.py validate
    gcp_verify_production_target
    gcp_verify_required_crds
  fi
  TX_STATE_ROOT="$(tx_state_root)"
  mkdir -p "${TX_STATE_ROOT}"
  tx_acquire_component_locks "${component}"
  tx_recover_component "${component}" "${TX_STATE_ROOT}"
  tx_release_component_locks
  exit 0
fi

if [[ "${DEPLOY_TARGET:-local}" == "gcp-production" ]]; then
  branch_name="${BRANCH_NAME:-${GIT_BRANCH:-}}"
  checked_out_main=0
  if git rev-parse --verify origin/main >/dev/null 2>&1 \
    && [[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]]; then
    checked_out_main=1
  fi
  if [[ "${branch_name}" != "main" && "${branch_name}" != "origin/main" ]] \
    && [[ "${checked_out_main}" != "1" ]] \
    && ! recsys_is_true "${FORCE_DEPLOY:-0}"; then
    recsys_error "GCP production deploy requires main, an origin/main checkout, or FORCE_DEPLOY=true; got ${branch_name:-<empty>}"
    exit 2
  fi
  recsys_is_true "${PUBLISH_IMAGES:-0}" || {
    recsys_error "GCP production deploy requires PUBLISH_IMAGES=true"
    exit 2
  }
  gcp_production_preflight
  verify_model_store_versioning_if_required
fi

tx_begin "${component}"
tx_transition SNAPSHOT
snapshot_component_releases "${component}"
snapshot_component_external_state "${component}"
tx_transition APPLYING
database_apply_component_migration "${component}"
deploy_component_dispatch "${component}"
tx_transition VERIFYING
if [[ "${DEPLOY_TARGET:-local}" == "gcp-production" ]]; then
  if component_test_run "${component}"; then
    tx_record_health_test \
      "${component}" passed "reports/junit/gcp-${component}.xml"
  else
    tx_record_health_test \
      "${component}" failed "reports/junit/gcp-${component}.xml"
    false
  fi
fi
tx_commit
