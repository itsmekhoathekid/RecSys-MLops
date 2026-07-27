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
