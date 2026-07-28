#!/usr/bin/env bash

database_component_policy() {
  python3 - "$1" <<'PY'
import json
import sys

payload = json.load(open("jenkins/config/components.json", encoding="utf-8"))
for component in payload["components"]:
    if component["name"] == sys.argv[1]:
        print(component["migrationPolicy"])
        raise SystemExit(0)
raise SystemExit(f"unknown component: {sys.argv[1]}")
PY
}

database_manifest_command() {
  local manifest_path="$1"
  local field="$2"
  python3 - "${manifest_path}" "${field}" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
print(payload.get(sys.argv[2], ""))
PY
}

database_run_manifest_command() {
  local manifest_path="$1"
  local field="$2"
  local command
  command="$(database_manifest_command "${manifest_path}" "${field}")"
  [[ -n "${command}" ]] || {
    recsys_error "database migration manifest is missing ${field}: ${manifest_path}"
    return 2
  }
  bash -euo pipefail -c "${command}"
}

database_apply_component_migration() {
  local component="$1"
  local policy
  local manifest_path="jenkins/config/migrations/${component}.json"
  local state_path
  local policy_args=(--component "${component}")
  policy="$(database_component_policy "${component}")"
  if [[ ! -f "${manifest_path}" ]]; then
    [[ "${policy}" != "reversible" ]] || {
      recsys_error "reversible migration manifest is required: ${manifest_path}"
      return 2
    }
    return 0
  fi
  if [[ -n "${CI_BASE_REF:-}" ]]; then
    policy_args+=(--base-ref "${CI_BASE_REF}")
  fi
  python3 -m jenkins.python.migration_policy "${policy_args[@]}"
  state_path="${TX_DIR}/database-migration.json"
  python3 - "${state_path}" "${manifest_path}" "${component}" "${policy}" <<'PY'
import json
import sys
from pathlib import Path

state_path, manifest_path, component, policy = sys.argv[1:]
Path(state_path).write_text(
    json.dumps(
        {
            "manifestPath": manifest_path,
            "component": component,
            "policy": policy,
            "applied": False,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  if [[ "${policy}" == "reversible" ]]; then
    tx_register_external database-migration "${state_path}"
  fi
  database_run_manifest_command "${manifest_path}" up
  database_run_manifest_command "${manifest_path}" verify
  database_run_manifest_command "${manifest_path}" oldImageCompatibility
  python3 - "${state_path}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["applied"] = True
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

database_rollback_migration() {
  local state_path="$1"
  local manifest_path applied
  [[ -s "${state_path}" ]] || return 0
  manifest_path="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["manifestPath"])' "${state_path}")"
  applied="$(python3 -c 'import json,sys; print("1" if json.load(open(sys.argv[1])).get("applied") else "0")' "${state_path}")"
  [[ "${applied}" == "1" ]] || return 0
  database_run_manifest_command "${manifest_path}" down
  database_run_manifest_command "${manifest_path}" verifyDown
}

database_snapshot_airflow_migration() {
  local namespace="$1"
  local migration_image="$2"
  local state_path="${TX_DIR}/airflow-database-migration.json"
  local current_image=""
  local previous_migration_revision=""
  local previous_version=""

  [[ "${TX_ACTIVE:-0}" == "1" ]] || return 0
  previous_migration_revision="$(
    database_airflow_migration_revision "${namespace}"
  )"
  [[ "${previous_migration_revision}" =~ ^[0-9a-f]+$ ]] || {
    recsys_error \
      "invalid previous Airflow migration revision: ${previous_migration_revision}"
    return 2
  }
  if kubectl get deployment/airflow-webserver -n "${namespace}" >/dev/null 2>&1; then
    previous_version="$(
      kubectl exec -n "${namespace}" deployment/airflow-webserver \
        -c airflow-webserver -- airflow version 2>/dev/null \
        | tail -n 1 \
        | tr -d '\r' || true
    )"
    if [[ ! "${previous_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-zA-Z0-9._+-]*)?$ ]]; then
      current_image="$(
        kubectl get deployment/airflow-webserver -n "${namespace}" \
          -o 'jsonpath={.spec.template.spec.containers[?(@.name=="airflow-webserver")].image}'
      )"
      [[ -n "${current_image}" ]] || {
        recsys_error "cannot resolve the current Airflow image in ${namespace}"
        return 2
      }
      previous_version="$(
        database_airflow_version_from_image "${namespace}" "${current_image}"
      )"
    fi
  fi
  [[ -n "${previous_version}" ]] || return 0
  [[ "${previous_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-zA-Z0-9._+-]*)?$ ]] || {
    recsys_error "invalid previous Airflow version: ${previous_version}"
    return 2
  }

  python3 - \
    "${state_path}" \
    "${namespace}" \
    "${migration_image}" \
    "${previous_version}" \
    "${previous_migration_revision}" <<'PY'
import json
import sys
from pathlib import Path

(
    state_path,
    namespace,
    migration_image,
    previous_version,
    previous_migration_revision,
) = sys.argv[1:]
Path(state_path).write_text(
    json.dumps(
        {
            "namespace": namespace,
            "migrationImage": migration_image,
            "previousVersion": previous_version,
            "previousMigrationRevision": previous_migration_revision,
            "secretName": "recsys-data-platform-secret",
            "attempted": False,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  tx_register_external airflow-database-migration "${state_path}"
}

database_airflow_migration_revision() {
  local namespace="$1"
  kubectl exec -n "${namespace}" airflow-postgres-0 -- \
    psql -U airflow -d airflow -Atc \
      "select version_num from alembic_version" \
    | tail -n 1 \
    | tr -d '\r'
}

database_airflow_version_from_image() {
  local namespace="$1"
  local image="$2"
  local pod_name
  local overrides
  local phase=""
  local previous_version=""
  local elapsed=0

  pod_name="$(recsys_kubernetes_name "airflow-version-${TX_ID}")"
  pod_name="${pod_name:0:63}"
  pod_name="${pod_name%-}"
  kubectl delete pod "${pod_name}" -n "${namespace}" \
    --ignore-not-found --wait=true >/dev/null
  overrides="$(
    python3 - "${pod_name}" "${image}" <<'PY'
import json
import sys

pod_name, image = sys.argv[1:]
print(
    json.dumps(
        {
            "metadata": {"annotations": {"sidecar.istio.io/inject": "false"}},
            "spec": {
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    {
                        "name": pod_name,
                        "image": image,
                        "imagePullPolicy": "Always",
                        "command": ["bash", "-lc"],
                        "args": ["airflow version"],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 50000,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
            },
        }
    )
)
PY
  )"
  if ! kubectl run "${pod_name}" -n "${namespace}" \
    --restart=Never \
    --image="${image}" \
    --overrides="${overrides}" >/dev/null; then
    recsys_error "cannot start Airflow version probe using ${image}"
    return 2
  fi
  while ((elapsed <= 180)); do
    phase="$(
      kubectl get pod "${pod_name}" -n "${namespace}" \
        -o jsonpath='{.status.phase}' 2>/dev/null || true
    )"
    case "${phase}" in
      Succeeded)
        break
        ;;
      Failed)
        kubectl logs -n "${namespace}" "${pod_name}" >&2 || true
        kubectl delete pod "${pod_name}" -n "${namespace}" \
          --ignore-not-found --wait=true >/dev/null
        recsys_error "Airflow version probe failed for ${image}"
        return 2
        ;;
    esac
    sleep 2
    elapsed=$((elapsed + 2))
  done
  if [[ "${phase}" != "Succeeded" ]]; then
    kubectl describe pod "${pod_name}" -n "${namespace}" >&2 || true
    kubectl delete pod "${pod_name}" -n "${namespace}" \
      --ignore-not-found --wait=true >/dev/null
    recsys_error "Airflow version probe timed out for ${image}"
    return 2
  fi
  previous_version="$(
    kubectl logs -n "${namespace}" "${pod_name}" \
      | tail -n 1 \
      | tr -d '\r'
  )"
  kubectl delete pod "${pod_name}" -n "${namespace}" \
    --ignore-not-found --wait=true >/dev/null
  printf '%s' "${previous_version}"
}

database_mark_airflow_migration_attempted() {
  local state_path="${TX_DIR}/airflow-database-migration.json"
  [[ "${TX_ACTIVE:-0}" == "1" && -n "${TX_DIR:-}" ]] || return 0
  [[ -s "${state_path}" ]] || return 0
  python3 - "${state_path}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["attempted"] = True
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY
}

database_rollback_airflow_migration() {
  local state_path="$1"
  local namespace migration_image previous_version previous_migration_revision
  local current_migration_revision secret_name attempted
  local pod_name overrides status=0
  local phase=""
  local elapsed=0
  local timeout_seconds="${COMPONENT_DEPLOY_TIMEOUT_SECONDS:-600}"

  [[ -s "${state_path}" ]] || return 0
  namespace="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["namespace"])' "${state_path}")"
  migration_image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["migrationImage"])' "${state_path}")"
  previous_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["previousVersion"])' "${state_path}")"
  previous_migration_revision="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("previousMigrationRevision", ""))' "${state_path}")"
  secret_name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["secretName"])' "${state_path}")"
  attempted="$(python3 -c 'import json,sys; print("1" if json.load(open(sys.argv[1])).get("attempted") else "0")' "${state_path}")"
  [[ "${attempted}" == "1" ]] || return 0
  if [[ -n "${previous_migration_revision}" \
    && ! "${previous_migration_revision}" =~ ^[0-9a-f]+$ ]]; then
    recsys_error \
      "invalid Airflow rollback revision: ${previous_migration_revision}"
    return 2
  fi
  current_migration_revision="$(
    database_airflow_migration_revision "${namespace}"
  )"
  if [[ -n "${previous_migration_revision}" \
    && "${current_migration_revision}" == "${previous_migration_revision}" ]]; then
    recsys_log \
      "Airflow metadata revision ${current_migration_revision} is unchanged; skipping database downgrade"
    return 0
  fi
  [[ "${previous_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([a-zA-Z0-9._+-]*)?$ ]] || {
    recsys_error "invalid Airflow rollback version: ${previous_version}"
    return 2
  }

  pod_name="$(recsys_kubernetes_name "airflow-db-rollback-${TX_ID}")"
  pod_name="${pod_name:0:63}"
  pod_name="${pod_name%-}"
  kubectl delete pod "${pod_name}" -n "${namespace}" --ignore-not-found --wait=true
  for deployment in airflow-dag-processor airflow-scheduler airflow-webserver; do
    if kubectl get "deployment/${deployment}" -n "${namespace}" >/dev/null 2>&1; then
      kubectl scale "deployment/${deployment}" -n "${namespace}" --replicas=0
    fi
  done
  kubectl wait -n "${namespace}" --for=delete pod \
    -l 'app in (airflow-dag-processor,airflow-scheduler,airflow-webserver)' \
    --timeout="${COMPONENT_DEPLOY_TIMEOUT:-600s}" || true

  overrides="$(
    python3 - \
      "${pod_name}" \
      "${migration_image}" \
      "${secret_name}" \
      "${previous_version}" \
      "${previous_migration_revision}" <<'PY'
import json
import sys

pod_name, image, secret_name, previous_version, previous_revision = sys.argv[1:]
downgrade_target = (
    f"--to-revision {previous_revision}"
    if previous_revision
    else f"--to-version {previous_version}"
)
print(
    json.dumps(
        {
            "metadata": {"annotations": {"sidecar.istio.io/inject": "false"}},
            "spec": {
                "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [
                    {
                        "name": pod_name,
                        "image": image,
                        "imagePullPolicy": "Always",
                        "command": ["bash", "-lc"],
                        "args": [
                            f"airflow db downgrade {downgrade_target} --yes"
                        ],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 50000,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                        "env": [
                            {
                                "name": "AIRFLOW_POSTGRES_USER",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": secret_name,
                                        "key": "AIRFLOW_POSTGRES_USER",
                                    }
                                },
                            },
                            {
                                "name": "AIRFLOW_POSTGRES_PASSWORD",
                                "valueFrom": {
                                    "secretKeyRef": {
                                        "name": secret_name,
                                        "key": "AIRFLOW_POSTGRES_PASSWORD",
                                    }
                                },
                            },
                            {
                                "name": "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN",
                                "value": (
                                    "postgresql+psycopg2://$(AIRFLOW_POSTGRES_USER):"
                                    "$(AIRFLOW_POSTGRES_PASSWORD)@airflow-postgres:5432/airflow"
                                ),
                            },
                        ],
                    }
                ]
            },
        }
    )
)
PY
  )"
  kubectl run "${pod_name}" -n "${namespace}" \
    --restart=Never \
    --image="${migration_image}" \
    --overrides="${overrides}"
  while ((elapsed <= timeout_seconds)); do
    phase="$(
      kubectl get pod "${pod_name}" -n "${namespace}" \
        -o jsonpath='{.status.phase}' 2>/dev/null || true
    )"
    case "${phase}" in
      Succeeded)
        break
        ;;
      Failed)
        status=1
        break
        ;;
    esac
    sleep 2
    elapsed=$((elapsed + 2))
  done
  [[ "${phase}" == "Succeeded" ]] || status=1
  kubectl logs -n "${namespace}" "${pod_name}" \
    >"${TX_DIR}/airflow-database-rollback.log" 2>&1 || true
  kubectl delete pod "${pod_name}" -n "${namespace}" --ignore-not-found --wait=true
  [[ "${status}" == "0" ]] || {
    recsys_error "Airflow metadata database downgrade to ${previous_version} failed"
    return 1
  }
  if [[ -n "${previous_migration_revision}" ]]; then
    current_migration_revision="$(
      database_airflow_migration_revision "${namespace}"
    )"
    [[ "${current_migration_revision}" == "${previous_migration_revision}" ]] || {
      recsys_error \
        "Airflow rollback revision mismatch: expected ${previous_migration_revision}, got ${current_migration_revision}"
      return 1
    }
  fi
  recsys_log "downgraded Airflow metadata database to ${previous_version}"
}
