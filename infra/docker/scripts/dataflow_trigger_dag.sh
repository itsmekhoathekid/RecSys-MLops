#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAG_ID="${1:-full_dataflow_local_dag}"

"${SCRIPT_DIR}/dataflow_compose.sh" exec airflow-webserver \
  airflow dags unpause "${DAG_ID}"

"${SCRIPT_DIR}/dataflow_compose.sh" exec airflow-webserver \
  airflow dags trigger "${DAG_ID}"
