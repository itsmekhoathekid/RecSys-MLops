#!/usr/bin/env bash
set -euo pipefail

namespace="${API_NAMESPACE:-api-serving}"
deployment="${API_DEPLOYMENT:-recsys-api-serving}"
configmap="${API_CONFIGMAP:-recsys-api-serving}"
rollout_timeout="${API_ROLLOUT_TIMEOUT:-300s}"

weight="$(kubectl get configmap "${configmap}" -n "${namespace}" -o jsonpath='{.data.AB_CANDIDATE_WEIGHT_PERCENT}')"
shadow="$(kubectl get configmap "${configmap}" -n "${namespace}" -o jsonpath='{.data.AB_SHADOW_ENABLED}')"
control_version="$(kubectl get configmap "${configmap}" -n "${namespace}" -o jsonpath='{.data.AB_CONTROL_MODEL_VERSION}')"
if [[ "${weight}" != "0" || "${shadow}" != "0" ]]; then
  echo "Champion-only verification failed: candidate weight=${weight}, shadow=${shadow}" >&2
  exit 1
fi

kubectl rollout status "deployment/${deployment}" -n "${namespace}" --timeout="${rollout_timeout}"

kubectl exec -n "${namespace}" "deployment/${deployment}" -c api -- \
  env EXPECTED_CONTROL_VERSION="${control_version}" python -c '
import collections, json, urllib.request
import os
variants = collections.Counter()
versions = collections.Counter()
for user_id in range(1, 41):
    body = json.dumps({"user_id": user_id, "candidate_item_ids": [1,2,3,4,5], "top_k": 3}).encode()
    request = urllib.request.Request("http://127.0.0.1:8080/recommendations", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode())
    variants[payload.get("ab_variant") or "control"] += 1
    versions[payload.get("model_version") or "unknown"] += 1
if variants.get("candidate", 0):
    raise SystemExit(f"candidate responses remained after rollback: {dict(variants)}")
expected = os.environ["EXPECTED_CONTROL_VERSION"]
if set(versions) != {expected}:
    raise SystemExit(f"non-champion model version remained after rollback: expected={expected}, actual={dict(versions)}")
print("champion-only variants", dict(variants))
print("champion-only model versions", dict(versions))
'
