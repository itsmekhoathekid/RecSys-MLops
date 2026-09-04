#!/usr/bin/env bash
set +e

if [[ -n "${CI_TMP_ROOT:-}" && -d "${CI_TMP_ROOT}" ]]; then
  rm -rf -- "${CI_TMP_ROOT}"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  jenkins/scripts/maintenance/docker_gc.sh
else
  printf '[POST] Docker daemon unavailable; skipping Docker GC.\n'
fi

jenkins/scripts/maintenance/uv_cache_gc.sh
