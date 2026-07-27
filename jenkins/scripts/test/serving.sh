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
        assert response.status == 200
        if path == "/version":
            assert json.load(response)["image_reference"] == sys.argv[1]
request = urllib.request.Request(
    "http://127.0.0.1:8080/recommendations",
    data=json.dumps({"user_id": 1, "candidate_item_ids": [1, 2], "top_k": 2}).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    payload = json.load(response)
    assert payload["user_id"] == 1
    assert payload["model_version"]
    assert isinstance(payload["items"], list)
with urllib.request.urlopen("http://127.0.0.1:8080/metrics", timeout=15) as response:
    assert "model_predictions_total" in response.read().decode()
' "$(image recsys-api-serving)"
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
