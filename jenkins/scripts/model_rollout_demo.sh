#!/usr/bin/env bash
set -euo pipefail

namespace="${ROLLOUT_WATCHER_NAMESPACE:-ci}"
deployment="${ROLLOUT_WATCHER_DEPLOYMENT:-recsys-model-rollout-watcher}"
container="${ROLLOUT_WATCHER_CONTAINER:-watcher}"
controller="/opt/recsys/apps/ml-system/src/cli/model_rollout_controller.py"

usage() {
  cat <<'EOF'
Usage:
  model_rollout_demo.sh mark <mlflow-registry-version> [versioned-manifest-uri]
  model_rollout_demo.sh watch-once
  model_rollout_demo.sh status <mlflow-registry-version>
  model_rollout_demo.sh traffic [request-count]
  model_rollout_demo.sh rollback <mlflow-registry-version>
EOF
}

controller_cli() {
  kubectl exec -n "${namespace}" "deployment/${deployment}" -c "${container}" -- \
    python "${controller}" "$@"
}

generate_traffic() {
  local count="${1:-2000}"
  kubectl exec -n api-serving deployment/recsys-api-serving -c api -- \
    env REQUEST_COUNT="${count}" python -c '
import collections
import json
import os
import urllib.request

count = int(os.environ["REQUEST_COUNT"])
variants = collections.Counter()
for user_id in range(1, count + 1):
    body = json.dumps({"user_id": user_id, "candidate_item_ids": [1, 2, 3, 4, 5], "top_k": 3}).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:8080/recommendations",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode())
    variants[payload.get("ab_variant") or "control"] += 1
print(json.dumps({"requests": count, "variants": dict(variants)}, sort_keys=True))
'
}

command="${1:-}"
case "${command}" in
  mark)
    mark_args=(mark --version "${2:?MLflow registry version is required}")
    [[ -n "${3:-}" ]] && mark_args+=(--manifest-uri "${3}")
    controller_cli "${mark_args[@]}"
    ;;
  watch-once)
    controller_cli watch --once
    ;;
  status)
    controller_cli status --version "${2:?MLflow registry version is required}"
    ;;
  traffic)
    generate_traffic "${2:-2000}"
    ;;
  rollback)
    controller_cli stage --version "${2:?MLflow registry version is required}" --stage rollback --weight 0
    ;;
  *)
    usage
    exit 2
    ;;
esac
