#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="${DATAFLOW_GENERATOR_RUN_ID:-test_10k_seed42}"
CONFIG="${DATAFLOW_GENERATOR_CONFIG:-configs/local/data_generator_test.yaml}"

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm spark-master \
  bash -lc "cd /opt/recsys && export PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys && \
    python3 apps/data-platform/data-generator/src/cli.py generate --config '${CONFIG}' && \
    /opt/spark/bin/spark-submit --master local[*] \
    apps/data-platform/src/ingest/batch_lakehouse_ingestion.py \
    --run-path 'apps/data-platform/data-generator/src/output/${RUN_ID}' \
    --run-id '${RUN_ID}' \
    --mode overwrite"
