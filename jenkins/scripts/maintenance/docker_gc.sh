#!/usr/bin/env bash
set -euo pipefail

case "${DOCKER_GC_ENABLED:-1}" in
  1|true|TRUE|yes|YES) ;;
  *) exit 0 ;;
esac

command -v docker >/dev/null 2>&1 || exit 0
command -v flock >/dev/null 2>&1 || {
  echo "[recsys-cicd] skip Docker GC because flock is unavailable"
  exit 0
}

lock_root="${JENKINS_HOME:-/tmp}/ci-build-locks"
retention="${DOCKER_GC_RETENTION:-168h}"
keep_storage="${DOCKER_GC_KEEP_STORAGE:-40GB}"
mkdir -p "${lock_root}"

(
  if ! flock -n 9; then
    echo "[recsys-cicd] skip Docker GC because another cleanup is active"
    exit 0
  fi
  docker image prune --force --filter "until=${retention}" || true
  docker builder prune --force \
    --filter "until=${retention}" \
    --keep-storage "${keep_storage}" || true
  docker system df || true
) 9>"${lock_root}/docker-gc.lock"
