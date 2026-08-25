#!/usr/bin/env bash
set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
tf_dir="${root_dir}/infra/terraform/gcp"
project_id="${GCP_PROJECT_ID:-recsys-mlops-506406}"
cluster="${GKE_CLUSTER:-recsys-mlops-gke}"
zone="${GKE_ZONE:-asia-southeast1-b}"
namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
airflow_deployment="${AIRFLOW_DEPLOYMENT:-airflow-webserver}"
airflow_container="${AIRFLOW_CONTAINER:-airflow-webserver}"
dag_id="${DRIFT_DAG_ID:-recsys_feature_drift_monitoring}"
timeout_seconds="${TRAINING_TIMEOUT_SECONDS:-21600}"
poll_seconds="${TRAINING_POLL_SECONDS:-20}"
report_dir="${root_dir}/reports/gcp/training"

[[ "$(gcloud config get-value project 2>/dev/null)" == "${project_id}" ]] || {
  echo "Refusing to train: active gcloud project is not ${project_id}" >&2
  exit 2
}

gcloud container clusters get-credentials "${cluster}" --zone "${zone}" --project "${project_id}" >/dev/null
compute_mode="$("${root_dir}/ops/gcp/terraform_gcp.sh" -chdir="${tf_dir}" output -raw ml_compute_mode 2>/dev/null || printf 'cpu')"
[[ "${compute_mode}" == "cpu" ]] || {
  echo "The existing drift retrain path currently defaults to CPU parameters; refusing compute mode ${compute_mode}." >&2
  exit 2
}

kubectl rollout status "deployment/${airflow_deployment}" -n "${namespace}" --timeout=600s
kubectl exec -n "${namespace}" "deployment/${airflow_deployment}" -c "${airflow_container}" -- \
  airflow dags list --output plain | grep -Fq "${dag_id}"

config_map="recsys-data-platform-config"
original_threshold="$(kubectl get configmap "${config_map}" -n "${namespace}" -o jsonpath='{.data.RETRAIN_PSI_THRESHOLD}')"
original_retrain="$(kubectl get configmap "${config_map}" -n "${namespace}" -o jsonpath='{.data.RETRAIN_ON_DRIFT}')"

restore_drift_config() {
  kubectl patch configmap "${config_map}" -n "${namespace}" --type merge \
    -p "{\"data\":{\"RETRAIN_PSI_THRESHOLD\":\"${original_threshold}\",\"RETRAIN_ON_DRIFT\":\"${original_retrain}\"}}" >/dev/null 2>&1 || true
}
trap restore_drift_config EXIT

airflow_state() {
  local run_id="$1"
  kubectl exec -n "${namespace}" "deployment/${airflow_deployment}" -c "${airflow_container}" -- \
    airflow dags list-runs --dag-id "${dag_id}" --output json 2>/dev/null \
    | jq -r --arg run_id "${run_id}" '.[] | select(.run_id == $run_id) | .state' \
    | tail -n 1
}

wait_airflow_run() {
  local run_id="$1"
  local deadline=$((SECONDS + timeout_seconds))
  local state=""
  while ((SECONDS < deadline)); do
    state="$(airflow_state "${run_id}")"
    case "${state}" in
      success) return 0 ;;
      failed) echo "Airflow drift run failed: ${run_id}" >&2; return 1 ;;
    esac
    sleep "${poll_seconds}"
  done
  echo "Timed out waiting for Airflow drift run ${run_id}; last state=${state:-unknown}" >&2
  return 1
}

trigger_airflow_run() {
  local run_id="$1"
  kubectl exec -n "${namespace}" "deployment/${airflow_deployment}" -c "${airflow_container}" -- \
    airflow dags trigger "${dag_id}" --run-id "${run_id}"
  wait_airflow_run "${run_id}"
}

timestamp="$(date -u +%Y%m%d-%H%M%S)"
bootstrap_run="gcp-bootstrap-drift-${timestamp}"
forced_run="gcp-force-retrain-${timestamp}"

# First pass ensures a reference baseline exists but cannot submit a training run.
kubectl patch configmap "${config_map}" -n "${namespace}" --type merge \
  -p '{"data":{"RETRAIN_ON_DRIFT":"false"}}' >/dev/null
trigger_airflow_run "${bootstrap_run}"

workflows_before="$(kubectl get workflows.argoproj.io -n kubeflow -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)"

# PSI is non-negative, so a negative threshold deterministically exercises the
# existing drift -> trigger_kubeflow_retrain path without changing application code.
kubectl patch configmap "${config_map}" -n "${namespace}" --type merge \
  -p '{"data":{"RETRAIN_PSI_THRESHOLD":"-1","RETRAIN_ON_DRIFT":"true"}}' >/dev/null
trigger_airflow_run "${forced_run}"

workflow=""
deadline=$((SECONDS + 600))
while ((SECONDS < deadline)); do
  workflows_after="$(kubectl get workflows.argoproj.io -n kubeflow -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)"
  workflow="$(comm -13 <(printf '%s\n' "${workflows_before}") <(printf '%s\n' "${workflows_after}") | tail -n 1)"
  [[ -n "${workflow}" ]] && break
  sleep "${poll_seconds}"
done
[[ -n "${workflow}" ]] || {
  echo "Drift DAG succeeded but no new Kubeflow workflow was created." >&2
  exit 1
}

deadline=$((SECONDS + timeout_seconds))
phase=""
while ((SECONDS < deadline)); do
  phase="$(kubectl get workflow "${workflow}" -n kubeflow -o jsonpath='{.status.phase}')"
  case "${phase}" in
    Succeeded) break ;;
    Failed|Error) echo "Kubeflow workflow ${workflow} failed with phase ${phase}." >&2; exit 1 ;;
  esac
  sleep "${poll_seconds}"
done
[[ "${phase}" == "Succeeded" ]] || {
  echo "Timed out waiting for Kubeflow workflow ${workflow}; last phase=${phase:-unknown}." >&2
  exit 1
}

mkdir -p "${report_dir}"
jq -n \
  --arg compute_mode "${compute_mode}" \
  --arg bootstrap_airflow_run "${bootstrap_run}" \
  --arg retrain_airflow_run "${forced_run}" \
  --arg kubeflow_workflow "${workflow}" \
  '{compute_mode:$compute_mode,bootstrap_airflow_run:$bootstrap_airflow_run,retrain_airflow_run:$retrain_airflow_run,kubeflow_workflow:$kubeflow_workflow,status:"Succeeded"}' \
  >"${report_dir}/drift-retrain-run.json"

ray_workflow_nodes="$(
  kubectl get workflow "${workflow}" -n kubeflow -o json \
    | jq '[.status.nodes[] | select(((.displayName // "") + " " + (.templateName // "")) | test("ray"; "i"))] | length'
)"
((ray_workflow_nodes > 0)) || {
  echo "Kubeflow workflow succeeded but contains no Ray task nodes: ${workflow}" >&2
  exit 1
}

kubectl get inferenceservice recsys-bst-triton -n kserve-triton-inference
# Successful KFP RayJobs have a short TTL and may already be gone by the time
# downstream evaluation, promotion, and KServe CD complete.
kubectl get rayjob -n kubeflow || echo "RayJobs already cleaned by their TTL; workflow Ray nodes succeeded."
echo "BST training completed through ${dag_id} in ${compute_mode} mode (workflow ${workflow})."
