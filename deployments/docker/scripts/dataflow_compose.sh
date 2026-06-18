#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/deployments/docker/docker-compose.dataflow.yml"
ENV_FILE="${REPO_ROOT}/deployments/docker/.env.dataflow"

exec docker compose \
  -f "${COMPOSE_FILE}" \
  --env-file "${ENV_FILE}" \
  "$@"
