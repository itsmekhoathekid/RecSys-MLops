#!/usr/bin/env bash
set -euo pipefail

plan_path="${1:-.ci-release-plan.json}"
source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/config.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/lib/kubernetes.sh
source jenkins/scripts/lib/port_forward.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/test/runtime.sh
source jenkins/scripts/test/data_platform.sh
source jenkins/scripts/test/ml_platform.sh
source jenkins/scripts/test/serving.sh
source jenkins/scripts/test/demo.sh
source jenkins/scripts/test/analytics.sh
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
run_node_rebalance="${RUN_NODE_REBALANCE:-1}"
validate_node_rebalance="${VALIDATE_NODE_REBALANCE:-1}"
kfp_port_forward_pids=()
local_model_store_endpoint_result=""
trap 'cleanup_port_forwards; port_forward_cleanup' EXIT

while IFS= read -r component; do
  [[ -n "${component}" ]] || continue
  component_test_run "${component}"
done < <(
  python3 jenkins/python/release_plan.py plan-verifications --plan "${plan_path}"
)
