#!/usr/bin/env bash
set -euo pipefail

plan_path="${1:-.ci-release-plan.json}"
source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/config.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/deploy/datahub.sh
source jenkins/scripts/deploy/agentic.sh
source jenkins/scripts/test/runtime.sh
source jenkins/scripts/test/data_platform.sh
source jenkins/scripts/test/ml_platform.sh
source jenkins/scripts/test/serving.sh
source jenkins/scripts/test/demo.sh
source jenkins/scripts/test/analytics.sh
source jenkins/scripts/test/rag.sh
source jenkins/scripts/test/datahub.sh
source jenkins/scripts/test/agentic.sh
source jenkins/scripts/test/dispatch.sh

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
namespace_ci="${CI_NAMESPACE:-ci}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
kfp_port_forward_pids=()
kfp_upload_endpoint_result=""
local_model_store_endpoint_result=""
trap stop_runtime_port_forwards EXIT

declare -A completed_verifications=()
while IFS= read -r component; do
  [[ -n "${component}" ]] || continue
  verification_key="$(component_verification_key "${component}")"
  if [[ -n "${completed_verifications[${verification_key}]:-}" ]]; then
    printf 'Skipping %s verification; shared check %s already passed for %s.\n' \
      "${component}" \
      "${verification_key}" \
      "${completed_verifications[${verification_key}]}"
    continue
  fi
  verify_deployed_component "${component}"
  completed_verifications["${verification_key}"]="${component}"
done < <(
  python3 jenkins/python/release_plan.py plan-verifications --plan "${plan_path}"
)
