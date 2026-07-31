#!/usr/bin/env bash
set -euo pipefail

source jenkins/scripts/lib/common.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/deploy/serving.sh

namespace_kubeflow="${KUBEFLOW_NAMESPACE:-kubeflow}"
namespace_mlops="${MLOPS_NAMESPACE:-experiment-tracking}"
promotion_manifest_uri="${PROMOTION_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/latest.json}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
kfp_port_forward_pids=()
kfp_upload_endpoint_result=""
local_model_store_endpoint_result=""
trap stop_runtime_port_forwards EXIT

deploy_kserve_model_cd
