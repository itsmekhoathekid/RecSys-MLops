#!/usr/bin/env bash

if [[ -n "${RECSYS_JENKINS_COMMON_LOADED:-}" ]]; then
  return 0
fi
readonly RECSYS_JENKINS_COMMON_LOADED=1

recsys_log() {
  printf '[recsys-cicd] %s\n' "$*"
}

recsys_error() {
  printf '[recsys-cicd] ERROR: %s\n' "$*" >&2
}

recsys_is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

recsys_require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    recsys_error "required command is not available: $1"
    return 2
  }
}

recsys_retry() {
  local max_attempts="$1"
  local delay_seconds="$2"
  shift 2
  local attempt=1
  local status=0

  while true; do
    if "$@"; then
      return 0
    else
      status=$?
    fi
    if ((attempt >= max_attempts)); then
      recsys_error "command failed after ${attempt} attempts: $*"
      return "${status}"
    fi
    recsys_log "command failed on attempt ${attempt}/${max_attempts}; retrying in ${delay_seconds}s: $*"
    sleep "${delay_seconds}"
    attempt=$((attempt + 1))
  done
}

recsys_repo_root() {
  git rev-parse --show-toplevel
}

recsys_slug() {
  printf '%s' "$1" | tr -cs '[:alnum:]_.-' '-'
}

recsys_kubernetes_name() {
  printf '%s' "$1" \
    | tr '[:upper:]_.' '[:lower:]--' \
    | tr -cs '[:lower:][:digit:]-' '-' \
    | sed -e 's/^-*//' -e 's/-*$//'
}

recsys_sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

recsys_sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  else
    shasum -a 256 | awk '{print $1}'
  fi
}
