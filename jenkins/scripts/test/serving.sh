#!/usr/bin/env bash

test_online_feature_api() {
  local namespace="${API_NAMESPACE:-api-serving}"
  local deployment="${FEATURE_API_DEPLOYMENT:-recsys-online-feature-api}"
  local expected_image
  expected_image="$(resolve_release_image recsys-online-feature-api)"
  component_test_wait_deployment "${namespace}" "${deployment}"
  kubectl exec -n "${namespace}" "deploy/${deployment}" -c api -- \
    python -c '
import json
import sys
import urllib.request
for path in ("/healthz", "/ready", "/version", "/metrics"):
    with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=15) as response:
        if path == "/version" and json.load(response)["image_reference"] != sys.argv[1]:
            raise SystemExit("Feature API image mismatch")
payload = json.dumps({"user_id": 1001, "candidate_item_ids": [1, 2, 3], "top_k": 3}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8080/online-features",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    body = json.load(response)
    assert body["user_id"] == 1001
' "${expected_image}"
}

test_inference_api() {
  local namespace="${API_NAMESPACE:-api-serving}"
  local deployment="${API_DEPLOYMENT:-recsys-inference-api}"
  local expected_image
  expected_image="$(resolve_release_image recsys-inference-api)"
  component_test_wait_deployment "${namespace}" "${deployment}"
  kubectl exec -n "${namespace}" "deploy/${deployment}" -c api -- \
    python -c '
import json
import sys
import urllib.request
for path in ("/healthz", "/ready", "/version", "/metrics"):
    with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=15) as response:
        if path == "/version" and json.load(response)["image_reference"] != sys.argv[1]:
            raise SystemExit("Inference API image mismatch")
        if path == "/metrics" and "recsys_api_rollout_config_info" not in response.read().decode():
            raise SystemExit("Inference API startup metric is missing")
with urllib.request.urlopen(
    "http://recsys-online-feature-api.api-serving.svc.cluster.local/healthz",
    timeout=15,
):
    pass
payload = json.dumps({"user_id": 1001, "candidate_item_ids": [1, 2, 3], "top_k": 3}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8080/recommendations",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    body = json.load(response)
    assert body["user_id"] == 1001
' "${expected_image}"
}

test_kserve() {
  local namespace="${KSERVE_NAMESPACE:-kserve-triton-inference}"
  kubectl wait --for=condition=Ready inferenceservice/recsys-bst-triton \
    -n "${namespace}" --timeout="${COMPONENT_TEST_TIMEOUT:-600s}"
  component_test_wait_deployment "${namespace}" recsys-bst-triton-predictor
  if [[ -f .model-cd/deployed-model.json ]]; then
    python3 -c 'import json; p=json.load(open(".model-cd/deployed-model.json")); assert p["model_version"]; assert p["triton_storage_uri"]'
  fi
}
