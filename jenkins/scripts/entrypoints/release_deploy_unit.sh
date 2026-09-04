#!/usr/bin/env bash
set -euo pipefail

unit_name="${1:?deploy unit is required}"
plan_path="${2:-.ci-release-plan.json}"
[[ -f "${plan_path}" ]] || {
  printf 'release plan does not exist: %s\n' "${plan_path}" >&2
  exit 2
}

exec jenkins/scripts/deploy/release_unit_runtime.sh "${unit_name}" "${plan_path}"
