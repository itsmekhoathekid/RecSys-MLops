#!/usr/bin/env bash
set -euo pipefail

source jenkins/scripts/lib/common.sh
source jenkins/scripts/lib/config.sh
source jenkins/scripts/lib/helm.sh
source jenkins/scripts/lib/image_manifest.sh
source jenkins/scripts/lib/kubernetes.sh
source jenkins/scripts/lib/port_forward.sh
source jenkins/scripts/deploy/runtime.sh
source jenkins/scripts/deploy/serving.sh

image_registry="${IMAGE_PULL_REGISTRY:-${IMAGE_REGISTRY:-$(python3 jenkins/python/configuration.py gcp imageRegistry)}}"
image_registry="${image_registry%/}"
image_tag="${IMAGE_TAG:-${GIT_COMMIT:-$(git rev-parse HEAD)}}"
namespace_api="${API_NAMESPACE:-api-serving}"
namespace_kserve="${KSERVE_NAMESPACE:-kserve-triton-inference}"
namespace_kubeflow="${KUBEFLOW_NAMESPACE:-kubeflow}"
namespace_mlops="${MLOPS_NAMESPACE:-experiment-tracking}"
promotion_manifest_uri="${PROMOTION_MANIFEST_URI:-s3://recsys-model-store/promotions/bst/latest.json}"
timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"
kfp_port_forward_pids=()
local_model_store_endpoint_result=""
trap 'cleanup_port_forwards; port_forward_cleanup' EXIT

deploy_kserve_model_cd
