#!/usr/bin/env bash
set -euo pipefail

storage_root="${JENKINS_STORAGE_ROOT:-/var/jenkins_home}"
[[ -d "${storage_root}" ]] || exit 0

read -r total_kb used_kb available_kb < <(
  df -Pk "${storage_root}" | awk 'NR == 2 { print $2, $3, $4 }'
)
[[ "${total_kb}" =~ ^[0-9]+$ && "${total_kb}" -gt 0 ]] || {
  printf '[PREFLIGHT] unable to inspect Jenkins storage at %s\n' "${storage_root}" >&2
  exit 2
}

used_percent=$((used_kb * 100 / total_kb))
minimum_available_kb=$((20 * 1024 * 1024))
if ((used_percent > 80 || available_kb < minimum_available_kb)); then
  printf '[PREFLIGHT] Jenkins PVC is unsafe: used=%s%% available=%sGi (requires <=80%% and >=20Gi).\n' \
    "${used_percent}" "$((available_kb / 1024 / 1024))" >&2
  exit 2
fi
printf '[PREFLIGHT] Jenkins PVC healthy: used=%s%% available=%sGi.\n' \
  "${used_percent}" "$((available_kb / 1024 / 1024))"
