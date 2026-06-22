#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VOLUME_FLAG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --volumes|-v)
      VOLUME_FLAG="--volumes"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

"${SCRIPT_DIR}/dataflow_compose.sh" down --remove-orphans ${VOLUME_FLAG}
