#!/usr/bin/env bash

test_analytics() {
  local namespace="${ANALYTICS_NAMESPACE:-analytics}"
  component_test_airflow_dag_registered recsys_analytics_daily
  component_test_wait_deployment "${namespace}" recsys-analytics-trino
  component_test_wait_deployment "${namespace}" recsys-analytics-superset
  kubectl exec -n "${namespace}" deploy/recsys-analytics-trino -- \
    curl -fsS http://127.0.0.1:8080/v1/info >/dev/null
  kubectl exec -n "${namespace}" deploy/recsys-analytics-superset -- \
    curl -fsS http://127.0.0.1:8088/health >/dev/null
}
