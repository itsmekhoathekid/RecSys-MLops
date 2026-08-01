#!/usr/bin/env bash

test_api() {
  local namespace="${API_NAMESPACE:-api-serving}"
  local deployment="${API_DEPLOYMENT:-recsys-api-serving}"
  component_test_wait_deployment "${namespace}" recsys-online-feature-api
  component_test_wait_deployment "${namespace}" "${deployment}"
  kubectl exec -n "${namespace}" "deploy/${deployment}" -c api -- \
    python -c '
import json
import sys
import urllib.request
for path in ("/healthz", "/ready", "/version"):
    with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=15) as response:
        if path == "/version":
            actual_image = json.load(response)["image_reference"]
            if actual_image != sys.argv[1]:
                raise SystemExit(
                    f"API image mismatch: expected {sys.argv[1]}, got {actual_image}"
                )
with urllib.request.urlopen("http://127.0.0.1:8080/metrics", timeout=15) as response:
    metrics = response.read().decode()
    if "recsys_api_rollout_config_info" not in metrics:
        raise SystemExit("API startup metric recsys_api_rollout_config_info is missing")
' "$(resolve_release_image recsys-api-serving)"
}

test_kserve() {
  local namespace="${KSERVE_NAMESPACE:-kserve-triton-inference}"
  kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton \
    -n "${namespace}" --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
  component_test_wait_deployment "${namespace}" recsys-bst-triton-predictor
  test_api
  if [[ -f .model-cd/deployed-model.json ]]; then
    python3 -c 'import json; p=json.load(open(".model-cd/deployed-model.json")); assert p["model_version"]; assert p["triton_storage_uri"]'
  fi
}
