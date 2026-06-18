#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PHASE="${1:-all}"

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm dataflow-cli \
  python deployments/docker/scripts/smoke_check_stack.py --phase "${PHASE}"
