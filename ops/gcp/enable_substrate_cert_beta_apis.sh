#!/usr/bin/env bash
set -Eeuo pipefail

project_id="${1:?usage: $0 PROJECT_ID CLUSTER_NAME LOCATION}"
cluster_name="${2:?usage: $0 PROJECT_ID CLUSTER_NAME LOCATION}"
cluster_location="${3:?usage: $0 PROJECT_ID CLUSTER_NAME LOCATION}"

required_apis=(
  "certificates.k8s.io/v1beta1/podcertificaterequests"
  "certificates.k8s.io/v1beta1/clustertrustbundles"
)

describe_cluster() {
  gcloud container clusters describe "${cluster_name}" \
    --project="${project_id}" \
    --location="${cluster_location}" \
    --format=json
}

cluster_json="$(describe_cluster)"
missing=()
for api in "${required_apis[@]}"; do
  if ! jq -e --arg api "${api}" \
    '.enableK8sBetaApis.enabledApis // [] | index($api) != null' \
    <<<"${cluster_json}" >/dev/null; then
    missing+=("${api}")
  fi
done

if ((${#missing[@]} == 0)); then
  echo "Substrate certificate beta APIs are already enabled on ${cluster_name}."
  exit 0
fi

echo "Enabling one-way Kubernetes beta APIs on ${cluster_name}: ${required_apis[*]}"
echo "GKE does not support disabling these APIs after enablement."
gcloud container clusters update "${cluster_name}" \
  --project="${project_id}" \
  --location="${cluster_location}" \
  --enable-kubernetes-unstable-apis="$(IFS=,; echo "${required_apis[*]}")"

cluster_json="$(describe_cluster)"
for api in "${required_apis[@]}"; do
  jq -e --arg api "${api}" \
    '.enableK8sBetaApis.enabledApis // [] | index($api) != null' \
    <<<"${cluster_json}" >/dev/null || {
      echo "GKE did not report enabled API ${api}" >&2
      exit 1
    }
done

echo "Substrate certificate beta APIs are enabled on ${cluster_name}."
