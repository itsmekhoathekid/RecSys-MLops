#!/usr/bin/env bash

component_test_wait_deployment() {
  kubectl rollout status \
    "deployment/$2" \
    -n "$1" \
    --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
}

component_test_airflow_dag_registered() {
  local dag_id="$1"
  local deadline=$((SECONDS + ${COMPONENT_TEST_TIMEOUT_SECONDS:-600}))

  while ((SECONDS < deadline)); do
    if kubectl exec -n "${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}" \
      deploy/airflow-webserver -c airflow-webserver -- \
      airflow dags list --output plain 2>/dev/null \
      | grep -Fq "${dag_id}"; then
      return 0
    fi
    sleep 5
  done
  recsys_error "Airflow DAG was not registered before verification timeout: ${dag_id}"
  return 1
}

verify_deployed_component() {
  local component="$1"
  local status=0
  local message=""
  local report_path="reports/junit/gcp-${component}.xml"
  local namespace=""
  mkdir -p "reports/gcp/${component}"

  set +e
  (
    set -euo pipefail
    run_component_verification "${component}"
  ) \
    > >(tee "reports/gcp/${component}/smoke.log") \
    2> >(tee "reports/gcp/${component}/smoke-error.log" >&2)
  status=$?
  set -e
  case "${component}" in
    materialize|training|dp1|dp2|dp3|drift|stream_offline|stream_online)
      namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
      ;;
    online_feature_api|inference_api|rag_api)
      namespace="${API_NAMESPACE:-api-serving}"
      ;;
    feature_rag_mcp|context_agent)
      namespace="${KAGENT_NAMESPACE:-kagent}"
      ;;
    kserve|kserve_model_cd)
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

component_ci_python() {
  local component="$1"
  local profile
  local python_path
  profile="$(python3 jenkins/python/configuration.py component-profile "${component}")"
  python_path="${CI_TMP_ROOT:?CI_TMP_ROOT is required}/envs/${profile}/bin/python"
  [[ -x "${python_path}" ]] || {
    recsys_error "locked CI Python is missing for ${component}: ${python_path}"
    return 2
  }
  printf '%s\n' "${python_path}"
}

component_test_http_from_deployment() {
  local namespace="$1"
  local deployment="$2"
  local container="$3"
  local url="$4"
  kubectl exec -n "${namespace}" "deploy/${deployment}" -c "${container}" -- \
    python -c 'import sys,urllib.request; assert urllib.request.urlopen(sys.argv[1], timeout=15).status == 200' "${url}"
}
