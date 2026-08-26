#!/usr/bin/env bash
set -Eeuo pipefail

expected_cluster="${1:?usage: $0 EXPECTED_CLUSTER_NAME}"
current_context="$(kubectl config current-context)"

[[ "${current_context}" == *"_${expected_cluster}" ]] || {
  echo "Refusing mTLS preparation: context ${current_context} is not ${expected_cluster}" >&2
  exit 2
}

preserved=0
for secret_name in actor-id-jwt-pool actor-id-ca-pool; do
  if kubectl get secret "${secret_name}" -n ate-system >/dev/null 2>&1; then
    kubectl get secret "${secret_name}" -n ate-system \
      -o jsonpath='{.data.pool}' | grep -q . || {
        echo "Existing Substrate identity pool ate-system/${secret_name} has no pool data" >&2
        exit 1
      }
    kubectl annotate secret "${secret_name}" -n ate-system \
      helm.sh/resource-policy=keep --overwrite
    preserved=$((preserved + 1))
  fi
done

echo "Preserved ${preserved} existing actor identity pool(s); missing pools will be created by the mTLS bootstrap chart."
