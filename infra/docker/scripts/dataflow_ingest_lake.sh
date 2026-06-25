#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUCKET="${1:-recsys-lakehouse}"
PREFIX="${2:-raw}"
RUN_ID="${DATAFLOW_GENERATOR_RUN_ID:-test_10k_seed42}"

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm dataflow-cli \
  bash -lc "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys python apps/data-platform/data-generator/src/scripts/generate_historical_to_minio.py \
    --target s3 \
    --bucket '${BUCKET}' \
    --prefix '${PREFIX}'"

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm spark-master \
  bash -lc "cd /opt/recsys && PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys /opt/spark/bin/spark-submit \
    apps/data-platform/src/ingest/batch_lakehouse_ingestion.py \
    --run-path 's3a://${BUCKET}/${PREFIX}/${RUN_ID}' \
    --mode overwrite"
