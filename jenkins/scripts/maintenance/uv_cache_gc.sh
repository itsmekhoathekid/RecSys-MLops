#!/usr/bin/env bash
set +e

cache_dir="${UV_CACHE_DIR:-/var/jenkins_home/caches/uv}"
maximum_gib="${UV_CACHE_MAX_GIB:-10}"
[[ -d "${cache_dir}" ]] || exit 0
size_kb="$(du -sk "${cache_dir}" 2>/dev/null | awk '{print $1}')"
[[ "${size_kb}" =~ ^[0-9]+$ ]] || exit 0
maximum_kb=$((maximum_gib * 1024 * 1024))
((size_kb <= maximum_kb)) && exit 0

printf '[POST] UV cache exceeds %sGi; pruning unused artifacts.\n' "${maximum_gib}"
UV_CACHE_DIR="${cache_dir}" uv cache prune --ci
size_kb="$(du -sk "${cache_dir}" 2>/dev/null | awk '{print $1}')"
if [[ "${size_kb}" =~ ^[0-9]+$ ]] && ((size_kb > maximum_kb)); then
  printf '[POST] UV cache remains above its hard limit; clearing only the UV cache.\n'
  UV_CACHE_DIR="${cache_dir}" uv cache clean
fi
