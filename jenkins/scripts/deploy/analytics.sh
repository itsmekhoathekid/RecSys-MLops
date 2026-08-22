#!/usr/bin/env bash

analytics_release_image() {
  local image_name="$1" value_path="$2"
  local reference="" deployed_revision=""
  reference="$(image_manifest_lookup "${image_name}")"
  if [[ -z "${reference}" ]]; then
    deployed_revision="$(
      helm history recsys-analytics -n "${namespace_analytics}" -o json 2>/dev/null \
        | python3 -c '
import json, sys
history = json.load(sys.stdin)
deployed = [int(item["revision"]) for item in history if item.get("status") == "deployed"]
print(max(deployed) if deployed else "")
'
    )"
    [[ -n "${deployed_revision}" ]] || {
      recsys_error "no built image or deployed Analytics revision exists for ${image_name}"
      return 2
    }
    reference="$(
      helm get values recsys-analytics -n "${namespace_analytics}" \
        --revision "${deployed_revision}" -o json 2>/dev/null \
        | python3 -c '
import json, sys
payload = json.load(sys.stdin)
value = payload
for token in sys.argv[1].split("."):
    value = value.get(token, {}) if isinstance(value, dict) else {}
print(value if isinstance(value, str) else "")
' "${value_path}"
    )"
  fi
  [[ -n "${reference}" ]] || {
    recsys_error "Analytics image value ${value_path} is empty for ${image_name}"
    return 2
  }
  if [[ "${DEPLOY_TARGET:-gcp-production}" == "gcp-production" ]]; then
    registry_resolve_digest_reference "${reference}" "${image_registry}"
  else
    printf '%s' "${reference}"
  fi
}

deploy_analytics() {
  local secret_create=true
  local external_secret_enabled=false
  local spark_image dbt_image superset_image
  spark_image="$(analytics_release_image recsys-spark images.spark)"
  dbt_image="$(analytics_release_image recsys-analytics-dbt images.dbt)"
  superset_image="$(analytics_release_image recsys-analytics-superset images.superset)"
  if [[ "${ANALYTICS_EXTERNAL_SECRET_ENABLED:-1}" == "1" ]]; then
    secret_create=false
    external_secret_enabled=true
  elif [[ "${ANALYTICS_ALLOW_DEV_SECRETS:-0}" != "1" ]]; then
    kubectl get secret recsys-analytics-secret -n "${namespace_analytics}" >/dev/null
    secret_create=false
  fi
  helm_atomic_upgrade recsys-analytics infra/helm/recsys-analytics \
    "${namespace_analytics}" "${timeout}" \
    -f infra/helm/recsys-analytics/values-gcp.yaml \
    --reuse-values \
    --set "namespace=${namespace_analytics}" \
    --set "secrets.create=${secret_create}" \
    --set "externalSecret.enabled=${external_secret_enabled}" \
    --set "images.pullPolicy=Always" \
    --set-string "images.spark=${spark_image}" \
    --set-string "images.dbt=${dbt_image}" \
    --set-string "images.superset=${superset_image}"
  verify_and_wait_workload deployment recsys-analytics-superset "${namespace_analytics}" "${superset_image}"
  wait_rollout_if_exists deployment recsys-analytics-trino "${namespace_analytics}"
  wait_rollout_if_exists deployment recsys-lakehouse-thrift "${namespace_analytics}"
  wait_rollout_if_exists deployment recsys-analytics-redis "${namespace_analytics}"
  wait_rollout_if_exists statefulset recsys-analytics-catalog-postgres "${namespace_analytics}"
  wait_rollout_if_exists statefulset recsys-analytics-superset-postgres "${namespace_analytics}"
}
