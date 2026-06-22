#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="${1:-}"

if [[ -n "${SERVICE}" ]]; then
  "${SCRIPT_DIR}/dataflow_compose.sh" logs -f --tail=200 "${SERVICE}"
else
  "${SCRIPT_DIR}/dataflow_compose.sh" logs -f --tail=200
fi
