#!/usr/bin/env bash

FEAST_REGISTRY_POD=""

feast_registry_pod_name() {
  local action="$1"
  local name
  name="$(recsys_slug "feast-registry-${action}-${TX_ID:-manual}" | tr '[:upper:]' '[:lower:]')"
  name="${name:0:63}"
  printf '%s' "${name%-}"
}

feast_registry_stop_pod() {
  if [[ -n "${FEAST_REGISTRY_POD:-}" ]]; then
    kubectl delete pod "${FEAST_REGISTRY_POD}" \
      -n "${namespace_data}" --ignore-not-found --wait=true >/dev/null 2>&1 || true
    FEAST_REGISTRY_POD=""
  fi
}

feast_registry_start_pod() {
  local action="$1"
  local image_reference="$2"
  local overrides

  FEAST_REGISTRY_POD="$(feast_registry_pod_name "${action}")"
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
                        "env": [{"name": "IMAGE_REFERENCE", "value": image}],
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
}

feast_registry_exec() {
  kubectl exec -n "${namespace_data}" "${FEAST_REGISTRY_POD}" -- \
    bash -lc "$1"
}

feast_registry_snapshot() {
  local image_reference="$1"
  local state_path="${TX_DIR}/feast-sql-registry.json"
  local status=0

  feast_registry_start_pod snapshot "${image_reference}" || return
  feast_registry_exec '
    set -euo pipefail
    export FEAST_SQL_REGISTRY_URL="$(python -m feature_store.sql_registry_state url)"
    python -m feature_store.sql_registry_state snapshot \
      --project recsys \
      --image-reference "$IMAGE_REFERENCE"
  ' >"${state_path}" || status=$?
  feast_registry_stop_pod
  if [[ "${status}" != "0" || ! -s "${state_path}" ]]; then
    recsys_error "failed to snapshot Feast SQL registry"
    return 1
  fi
  tx_register_external feast-sql-registry "${state_path}"
}

feast_registry_plan_apply() {
  local image_reference="$1"
  local report_dir="reports/gcp/materialize"
  local log_path="${report_dir}/feast-plan-apply.log"
  local status=0

  mkdir -p "${report_dir}"
  feast_registry_start_pod apply "${image_reference}" || return
  feast_registry_exec '
    set -euo pipefail
    export FEAST_SQL_REGISTRY_URL="$(python -m feature_store.sql_registry_state url)"
    feast -c /opt/recsys/apps/data-platform/feature-store/feature_repo plan
    feast -c /opt/recsys/apps/data-platform/feature-store/feature_repo \
      apply --no-progress
    python -m feature_store.sql_registry_state verify --project recsys
  ' 2>&1 | tee "${log_path}" || status=${PIPESTATUS[0]}
  feast_registry_stop_pod
  if [[ "${status}" != "0" ]]; then
    recsys_error "Feast plan/apply failed; component transaction will roll back"
    return "${status}"
  fi
}

tx_restore_feast_sql_registry() {
  local state_path="$1"
  local image_reference
  local status=0

  [[ -s "${state_path}" ]] || {
    recsys_error "Feast SQL registry snapshot is missing: ${state_path}"
    return 1
  }
  image_reference="$(
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["imageReference"])' \
      "${state_path}"
  )"
  [[ -n "${image_reference}" ]] || {
    recsys_error "Feast SQL registry snapshot has no recovery image"
    return 1
  }

  feast_registry_start_pod restore "${image_reference}" || return
  if ! kubectl exec -i -n "${namespace_data}" "${FEAST_REGISTRY_POD}" -- \
    bash -lc 'cat > /tmp/feast-sql-registry.json' <"${state_path}"; then
    status=1
  elif ! feast_registry_exec '
    set -euo pipefail
    export FEAST_SQL_REGISTRY_URL="$(python -m feature_store.sql_registry_state url)"
    python -m feature_store.sql_registry_state restore \
      --state-path /tmp/feast-sql-registry.json
  '; then
    status=1
  fi
  feast_registry_stop_pod
  [[ "${status}" == "0" ]] || {
    recsys_error "failed to restore Feast SQL registry"
    return 1
  }
}
