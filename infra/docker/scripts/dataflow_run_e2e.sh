#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAG_ID="${1:-recsys_dp1_raw_to_bronze}"

"${SCRIPT_DIR}/dataflow_trigger_dag.sh" "${DAG_ID}"

cat <<EOF
Triggered ${DAG_ID}.

Airflow local:
  http://localhost:8088
EOF
