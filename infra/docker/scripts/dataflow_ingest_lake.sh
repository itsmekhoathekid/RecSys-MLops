#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUCKET="${1:-recsys-lake}"
PREFIX="${2:-raw}"

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm dataflow-cli \
  bash -lc "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py \
    --target s3 \
    --bucket '${BUCKET}' \
    --prefix '${PREFIX}'"

"${SCRIPT_DIR}/dataflow_smoke.sh" buckets
