#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAG_ID="${1:-full_dataflow_local_dag}"
SMOKE_PHASE="${2:-all}"

"${SCRIPT_DIR}/dataflow_trigger_dag.sh" "${DAG_ID}"

cat <<EOF
Triggered ${DAG_ID}.

Watch it in Airflow UI, then run:
  make dataflow-smoke DATAFLOW_SMOKE_PHASE=${SMOKE_PHASE}

Airflow local:
  http://localhost:8088
EOF
