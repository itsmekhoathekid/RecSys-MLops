#!/usr/bin/env bash

RECSYS_PORT_FORWARD_PIDS=()

port_forward_register_pid() {
  RECSYS_PORT_FORWARD_PIDS+=("$1")
}

port_forward_cleanup() {
  local pid
  for pid in "${RECSYS_PORT_FORWARD_PIDS[@]:-}"; do
    kill "${pid}" >/dev/null 2>&1 || true
  done
  RECSYS_PORT_FORWARD_PIDS=()
}

port_forward_wait() {
  local port="$1"
  local label="$2"
  local attempts="${3:-60}"
  local index
  for ((index = 0; index < attempts; index += 1)); do
    if (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  recsys_error "timed out waiting for ${label} on 127.0.0.1:${port}"
  return 1
}
