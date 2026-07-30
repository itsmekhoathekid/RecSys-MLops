#!/usr/bin/env bash

test_training() {
  local namespace="${MLOPS_NAMESPACE:-experiment-tracking}"
  local kfp_endpoint=""
  local smoke_run_id="ci-${BUILD_NUMBER:-manual}"
  component_test_wait_deployment "${namespace}" mlflow
  kubectl exec -n "${namespace}" deploy/mlflow -- \
    /opt/venv/bin/python -c '
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:5000/health", timeout=10) as response:
    assert response.status == 200
'
  if [[ -f ".ci-deploy/kfp-upload.json" ]]; then
    python3 -c '
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
assert payload.get("pipeline_id")
assert payload.get("pipeline_version_id") or payload.get("action") == "uploaded_pipeline"
' ".ci-deploy/kfp-upload.json"
  fi
  kfp_endpoint="$(kfp_endpoint_for_upload)"
  PYTHONPATH=apps/ml-system/src:apps/data-platform/src \
    runtime_python apps/ml-system/src/kubeflow/submit_pipeline_run.py \
    --host "${kfp_endpoint}" \
    --package-path pipelines/kubeflow/compiled/bst_training_pipeline.yaml \
    --experiment-name recsys-ci-smoke \
    --run-name "${smoke_run_id}" \
    --argument "pipeline_run_id=${smoke_run_id}" \
    --argument 'training_percent=0.01' \
    --argument 'distributed_training_percent=0.01' \
    --argument 'max_trials=1' \
    --argument 'parallel_trials=1' \
    --argument 'num_epochs=1' \
    --argument 'distributed_num_epochs=1' \
    --argument 'worker_replicas=1' \
    --argument 'distributed_worker_replicas=1' \
    --argument 'distributed_num_workers=1' \
    --argument 'kserve_cd_score_threshold=999.0' \
    --timeout-seconds "${KFP_SMOKE_TIMEOUT_SECONDS:-1800}" \
    --poll-seconds 15 \
    | tee "reports/gcp/training/kfp-smoke-run.json"
  kubectl exec -n "${namespace}" deploy/mlflow -- \
    /opt/venv/bin/python -c '
import json
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:5000/api/2.0/mlflow/experiments/search?max_results=100", timeout=15) as response:
    payload = json.load(response)
    assert payload.get("experiments")
'
}

test_mlflow() {
  local namespace="${MLOPS_NAMESPACE:-experiment-tracking}"
  component_test_wait_deployment "${namespace}" mlflow
  kubectl exec -n "${namespace}" deploy/mlflow -- \
    /opt/venv/bin/python -c '
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:5000/health", timeout=10) as response:
    assert response.status == 200
'
}

test_rollout() {
  component_test_wait_deployment "${CI_NAMESPACE:-ci}" recsys-model-rollout-watcher
  kubectl exec -n "${CI_NAMESPACE:-ci}" deploy/recsys-model-rollout-watcher -- \
    python -c '
import base64
import os
import urllib.request

url = os.environ["JENKINS_URL"].rstrip("/")
user = os.environ["JENKINS_USER"]
token = os.environ["JENKINS_TOKEN"]
headers = {
    "Authorization": "Basic "
    + base64.b64encode(f"{user}:{token}".encode()).decode()
}
expected = {
    "RecSys-Progressive-Rollout-CICD": "Jenkinsfile",
    os.environ["KSERVE_CD_JOB_NAME"]: "jenkins/KServeModelCD.Jenkinsfile",
}
for job, scm_path in expected.items():
    request = urllib.request.Request(f"{url}/job/{job}/config.xml", headers=headers)
    config = urllib.request.urlopen(request, timeout=15).read().decode()
    assert scm_path in config, (job, scm_path)
'
}
