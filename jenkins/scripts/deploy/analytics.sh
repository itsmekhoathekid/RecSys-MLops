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
  deploy_data_platform \
    --set "images.airflow=$(image recsys-airflow)" \
    --set "images.analyticsSpark=$(image recsys-analytics-spark)" \
    --set "images.analyticsDbt=$(image recsys-analytics-dbt)"
  verify_and_wait_workload deployment airflow-scheduler "${namespace_data}" "$(image recsys-airflow)"
  verify_and_wait_workload deployment airflow-dag-processor "${namespace_data}" "$(image recsys-airflow)"
  helm_atomic_upgrade recsys-analytics infra/helm/recsys-analytics \
    "${namespace_analytics}" "${timeout}" \
    --reuse-values \
    --set "namespace=${namespace_analytics}" \
    --set "secrets.create=${secret_create}" \
    --set "externalSecret.enabled=${external_secret_enabled}" \
    --set "images.pullPolicy=Always" \
    --set "images.spark=$(image recsys-analytics-spark)" \
    --set "images.dbt=$(image recsys-analytics-dbt)" \
    --set "images.superset=$(image recsys-analytics-superset)"
  verify_and_wait_workload deployment recsys-analytics-superset "${namespace_analytics}" "$(image recsys-analytics-superset)"
  wait_rollout_if_exists deployment recsys-analytics-trino "${namespace_analytics}"
  wait_rollout_if_exists deployment recsys-analytics-redis "${namespace_analytics}"
  wait_rollout_if_exists statefulset recsys-analytics-catalog-postgres "${namespace_analytics}"
  wait_rollout_if_exists statefulset recsys-analytics-superset-postgres "${namespace_analytics}"
}
