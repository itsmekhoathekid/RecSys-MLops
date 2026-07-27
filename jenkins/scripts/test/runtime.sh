#!/usr/bin/env bash

component_test_wait_deployment() {
  kubectl rollout status \
    "deployment/$2" \
    -n "$1" \
    --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
}

component_test_airflow_dag() {
  local dag_id="$1"
  local run_id="ci-${TX_ID:-${BUILD_NUMBER:-manual}}"
  local state=""
  local deadline=$((SECONDS + ${COMPONENT_TEST_TIMEOUT_SECONDS:-600}))

  kubectl exec -n "${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}" \
    deploy/airflow-webserver -c airflow-webserver -- \
    airflow dags unpause "${dag_id}" >/dev/null
  kubectl exec -n "${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}" \
    deploy/airflow-webserver -c airflow-webserver -- \
    airflow dags trigger "${dag_id}" --run-id "${run_id}" >/dev/null

  while ((SECONDS < deadline)); do
    state="$(
      kubectl exec -n "${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}" \
        deploy/airflow-webserver -c airflow-webserver -- \
        airflow dags state "${dag_id}" "${run_id}" 2>/dev/null \
        | tail -n 1 \
        | tr -d '\r'
    )"
    case "${state}" in
      success) return 0 ;;
      failed|upstream_failed)
        recsys_error "Airflow smoke ${dag_id}/${run_id} failed"
        return 1
        ;;
    esac
    sleep 10
  done
  recsys_error "Airflow smoke ${dag_id}/${run_id} timed out; last state=${state:-unknown}"
  return 1
}

component_test_run() {
  local component="$1"
  local status=0
  local message=""
  local report_path="reports/junit/gcp-${component}.xml"
  local namespace=""
  mkdir -p "reports/gcp/${component}"

  set +e
  component_test_dispatch "${component}" \
    > >(tee "reports/gcp/${component}/smoke.log") \
    2> >(tee "reports/gcp/${component}/smoke-error.log" >&2)
  status=$?
  set -e
  case "${component}" in
    materialize|training|spark_batch|dp1|dp2|dp3|drift|stream_offline|stream_online)
      namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
      ;;
    api|kserve|kserve_model_cd)
      namespace="${KSERVE_NAMESPACE:-kserve-triton-inference}"
      ;;
    rollout) namespace="${CI_NAMESPACE:-ci}" ;;
    analytics) namespace="${ANALYTICS_NAMESPACE:-analytics}" ;;
    demo_web) namespace="${DEMO_WEB_NAMESPACE:-api-serving}" ;;
    mlflow) namespace="${MLOPS_NAMESPACE:-experiment-tracking}" ;;
  esac
  if [[ -n "${namespace}" ]]; then
    kubectl get deploy,statefulset,daemonset,job,pod -n "${namespace}" -o wide \
      >"reports/gcp/${component}/workloads.txt" 2>&1 || true
    kubectl get events -n "${namespace}" --sort-by=.lastTimestamp \
      >"reports/gcp/${component}/events.txt" 2>&1 || true
    helm list -n "${namespace}" \
      >"reports/gcp/${component}/helm-releases.txt" 2>&1 || true
  fi
  if [[ "${status}" != "0" ]]; then
    message="GCP production smoke failed for ${component}"
  fi
  python3 jenkins/python/test_report.py \
    --path "${report_path}" \
    --component "${component}" \
    --status "${status}" \
    --message "${message}"
  return "${status}"
}

component_test_http_from_deployment() {
  local namespace="$1"
  local deployment="$2"
  local container="$3"
  local url="$4"
  kubectl exec -n "${namespace}" "deploy/${deployment}" -c "${container}" -- \
    python -c 'import sys,urllib.request; assert urllib.request.urlopen(sys.argv[1], timeout=15).status == 200' "${url}"
}

component_test_verify_rollback() {
  case "$1" in
    materialize|spark_batch|dp1|dp2|dp3|drift|stream_offline|stream_online|training)
      test_data_platform_base
      ;;
    api)
      component_test_http_from_deployment \
        "${API_NAMESPACE:-api-serving}" recsys-api-serving api http://127.0.0.1:8080/ready
      ;;
    kserve|kserve_model_cd)
      kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton \
        -n "${KSERVE_NAMESPACE:-kserve-triton-inference}" \
        --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
      ;;
    rollout)
      component_test_wait_deployment "${CI_NAMESPACE:-ci}" recsys-model-rollout-watcher
      ;;
    analytics)
      component_test_wait_deployment "${ANALYTICS_NAMESPACE:-analytics}" recsys-analytics-trino
      component_test_wait_deployment "${ANALYTICS_NAMESPACE:-analytics}" recsys-analytics-superset
      ;;
    demo_web)
      component_test_http_from_deployment \
        "${DEMO_WEB_NAMESPACE:-api-serving}" recsys-demo-api backend http://127.0.0.1:8080/ready
      ;;
    mlflow)
      component_test_wait_deployment "${MLOPS_NAMESPACE:-experiment-tracking}" mlflow
      ;;
  esac
}
