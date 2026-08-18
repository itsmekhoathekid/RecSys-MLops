#!/usr/bin/env bash

deploy_analytics() {
  local secret_create=true
  local external_secret_enabled=false
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
    --set "images.spark=$(resolve_release_image recsys-spark)" \
    --set "images.dbt=$(resolve_release_image recsys-analytics-dbt)" \
    --set "images.superset=$(resolve_release_image recsys-analytics-superset)"
  verify_and_wait_workload deployment recsys-analytics-superset "${namespace_analytics}" "$(resolve_release_image recsys-analytics-superset)"
  wait_rollout_if_exists deployment recsys-analytics-trino "${namespace_analytics}"
  wait_rollout_if_exists deployment recsys-lakehouse-thrift "${namespace_analytics}"
  wait_rollout_if_exists deployment recsys-analytics-redis "${namespace_analytics}"
  wait_rollout_if_exists statefulset recsys-analytics-catalog-postgres "${namespace_analytics}"
  wait_rollout_if_exists statefulset recsys-analytics-superset-postgres "${namespace_analytics}"
}
