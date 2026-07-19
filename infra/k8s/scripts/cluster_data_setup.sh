#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PROFILE="${MINIKUBE_PROFILE:-recsys-mlops}"
KUBE_CONTEXT="${KUBE_CONTEXT:-${PROFILE}}"
NAMESPACE="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
DAG_IDS_VALUE="${RECSYS_DATA_SETUP_DAG_IDS:-recsys_dp1_raw_to_bronze recsys_dp2_bronze_to_silver_gold recsys_dp3_offline_feature_table}"
read -r -a DAG_IDS <<<"${DAG_IDS_VALUE}"
RUN_ID_PREFIX="${RECSYS_DATA_SETUP_RUN_ID_PREFIX:-data-setup-$(date -u +%Y%m%d%H%M%S)}"
WAIT_TIMEOUT_SECONDS="${RECSYS_DATA_SETUP_TIMEOUT_SECONDS:-3600}"
POLL_SECONDS="${RECSYS_DATA_SETUP_POLL_SECONDS:-20}"
SKIP_CLUSTER_UP="${RECSYS_DATA_SETUP_SKIP_CLUSTER_UP:-0}"
SKIP_INSTALL="${RECSYS_DATA_SETUP_SKIP_INSTALL:-0}"

section() {
  printf "\n== %s ==\n" "$1"
}

run_make() {
  make -C "${ROOT_DIR}" "$@"
}

airflow() {
  kubectl exec -n "${NAMESPACE}" deploy/airflow-webserver -- airflow "$@"
}

airflow_run_state() {
  local dag_id="$1" run_id="$2"
  airflow dags list-runs -d "${dag_id}" --output json \
    | python3 -c 'import json,sys; run_id=sys.argv[1]; runs=json.load(sys.stdin); print(next((run.get("state", "") for run in runs if run.get("run_id") == run_id), ""))' "${run_id}"
}

wait_for_airflow_run() {
  local dag_id="$1" run_id="$2"
  local elapsed=0
  local state=""
  section "Wait Airflow DAG Run"
  while (( elapsed <= WAIT_TIMEOUT_SECONDS )); do
    state="$(airflow_run_state "${dag_id}" "${run_id}" || true)"
    echo "${dag_id}/${run_id}: ${state:-unknown}"
    case "${state}" in
      success)
        return 0
        ;;
      failed)
        airflow dags list-runs -d "${dag_id}" || true
        return 1
        ;;
    esac
    sleep "${POLL_SECONDS}"
    elapsed=$((elapsed + POLL_SECONDS))
  done
  echo "Timed out waiting for ${dag_id}/${run_id} after ${WAIT_TIMEOUT_SECONDS}s"
  airflow dags list-runs -d "${dag_id}" || true
  return 1
}

if [[ "${SKIP_CLUSTER_UP}" == "1" ]]; then
  section "Use Existing Full Service Cluster"
  kubectl config use-context "${KUBE_CONTEXT}" >/dev/null || true
else
  section "Start Full Service Cluster"
  MINIKUBE_PROFILE="${PROFILE}" run_make cluster-up
fi

section "Install And Wait Data Platform"
if [[ "${SKIP_INSTALL}" == "1" ]]; then
  echo "Skipping Helm install/upgrade for data platform; waiting for existing pods."
else
  run_make data-platform-install
fi
kubectl wait --for=condition=ready pod -l app=data-platform-minio -n "${NAMESPACE}" --timeout=240s
kubectl wait --for=condition=ready pod -l app=kafka -n "${NAMESPACE}" --timeout=240s
kubectl wait --for=condition=ready pod -l app=kafka-connect -n "${NAMESPACE}" --timeout=300s
kubectl wait --for=condition=ready pod -l app=source-postgres -n "${NAMESPACE}" --timeout=180s
kubectl wait --for=condition=ready pod -l app=redis -n "${NAMESPACE}" --timeout=180s
kubectl wait --for=condition=ready pod -l app=airflow-webserver -n "${NAMESPACE}" --timeout=240s

section "Trigger DP1 -> DP2 -> DP3 Data Setup DAGs"
for dag_id in "${DAG_IDS[@]}"; do
  run_id="${RUN_ID_PREFIX}-${dag_id}"
  airflow dags unpause "${dag_id}"
  if [[ -n "$(airflow_run_state "${dag_id}" "${run_id}" || true)" ]]; then
    echo "Airflow DAG run ${dag_id}/${run_id} already exists; waiting for it."
  else
    airflow dags trigger "${dag_id}" --run-id "${run_id}"
  fi
  wait_for_airflow_run "${dag_id}" "${run_id}"
done

section "Verify Feature Store And Redis Online Store"
run_make data-platform-verify-e2e

section "Data Setup Complete"
run_make data-platform-run-status
