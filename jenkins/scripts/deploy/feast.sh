#!/usr/bin/env bash

FEAST_REGISTRY_POD=""

feast_registry_stop_pod() {
  if [[ -n "${FEAST_REGISTRY_POD:-}" ]]; then
    kubectl delete pod "${FEAST_REGISTRY_POD}" \
      -n "${namespace_data}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    FEAST_REGISTRY_POD=""
  fi
}

feast_registry_start_pod() {
  local image_reference="$1"
  local overrides
  local actual_image

  FEAST_REGISTRY_POD="$(
    recsys_kubernetes_name "feast-registry-apply-${BUILD_NUMBER:-manual}"
  )"
  FEAST_REGISTRY_POD="${FEAST_REGISTRY_POD:0:63}"
  FEAST_REGISTRY_POD="${FEAST_REGISTRY_POD%-}"
  kubectl delete pod "${FEAST_REGISTRY_POD}" \
    -n "${namespace_data}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
  overrides="$(
    python3 - "${FEAST_REGISTRY_POD}" "${image_reference}" <<'PY'
import json
import sys

name, image = sys.argv[1:]
print(
    json.dumps(
        {
            "metadata": {"annotations": {"sidecar.istio.io/inject": "false"}},
            "spec": {
                "restartPolicy": "Never",
                "containers": [
                    {
                        "name": name,
                        "image": image,
                        "command": ["sleep", "1800"],
                        "envFrom": [
                            {"configMapRef": {"name": "recsys-data-platform-config"}},
                            {"secretRef": {"name": "recsys-data-platform-secret"}},
                        ],
                    }
                ],
            },
        }
    )
)
PY
  )"
  kubectl run "${FEAST_REGISTRY_POD}" \
    -n "${namespace_data}" \
    --restart=Never \
    --image="${image_reference}" \
    --overrides="${overrides}"
  if ! kubectl wait -n "${namespace_data}" \
    --for=condition=Ready "pod/${FEAST_REGISTRY_POD}" \
    --timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}"; then
    kubectl describe pod "${FEAST_REGISTRY_POD}" -n "${namespace_data}" || true
    feast_registry_stop_pod
    return 1
  fi
  actual_image="$(
    kubectl get pod "${FEAST_REGISTRY_POD}" -n "${namespace_data}" \
      -o jsonpath='{.spec.containers[0].image}'
  )"
  [[ "${actual_image}" == "${image_reference}" ]] || {
    recsys_error "Feast registry pod image mismatch: expected ${image_reference}, got ${actual_image}"
    feast_registry_stop_pod
    return 1
  }
}

feast_registry_apply() {
  local image_reference="$1"
  local report_dir="reports/gcp/materialize"
  local log_path="${report_dir}/feast-plan-apply.log"
  local status=0

  mkdir -p "${report_dir}"
  feast_registry_start_pod "${image_reference}" || return
  kubectl exec -n "${namespace_data}" "${FEAST_REGISTRY_POD}" -- bash -lc '
    set -euo pipefail
    export FEAST_SQL_REGISTRY_URL="$(/opt/venv/bin/python -m feature_store.sql_registry_state url)"
    /opt/venv/bin/feast -c /opt/recsys/apps/data-platform/feature-store/feature_repo plan
    /opt/venv/bin/feast -c /opt/recsys/apps/data-platform/feature-store/feature_repo \
      apply --no-progress
    /opt/venv/bin/python -m feature_store.sql_registry_state verify --project recsys
  ' 2>&1 | tee "${log_path}" || status=${PIPESTATUS[0]}
  feast_registry_stop_pod
  [[ "${status}" == "0" ]] || {
    recsys_error "Feast plan/apply failed"
    return "${status}"
  }
}
