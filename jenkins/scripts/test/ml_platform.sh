#!/usr/bin/env bash

test_training() {
  local namespace="${MLOPS_NAMESPACE:-experiment-tracking}"
  local kfp_endpoint=""
  local training_python=""
  component_test_wait_deployment "${namespace}" mlflow
  kubectl exec -n "${namespace}" deploy/mlflow -- \
    /opt/venv/bin/python -c '
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:5000/health", timeout=10) as response:
    assert response.status == 200
'
  [[ -s ".ci-deploy/kfp-upload.json" ]] || {
    recsys_error "Kubeflow upload result is missing"
    return 2
  }
  open_kfp_upload_endpoint
  kfp_endpoint="${kfp_upload_endpoint_result}"
  training_python="$(component_ci_python training)"
  PYTHONPATH=apps/ml-system/src:apps/data-platform/src \
    "${training_python}" apps/ml-system/src/kubeflow/verify_pipeline_upload.py \
    --host "${kfp_endpoint}" \
    --state-path .ci-deploy/kfp-upload.json \
    | tee "reports/gcp/training/kfp-package-verification.json"
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
