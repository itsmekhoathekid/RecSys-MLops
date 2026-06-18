#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCER_NAME="${DATAFLOW_REALTIME_PRODUCER_NAME:-recsys-dataflow-realtime-producer}"
FLINK_NAME="${DATAFLOW_REALTIME_FLINK_NAME:-recsys-dataflow-realtime-flink}"
INTERVAL_SECONDS="${DATAFLOW_REALTIME_INTERVAL_SECONDS:-2}"
EVENTS_PER_TICK="${DATAFLOW_REALTIME_EVENTS_PER_TICK:-5}"

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm dataflow-cli \
  python deployments/docker/scripts/init_postgres_schema.py

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm dataflow-cli \
  bash deployments/docker/scripts/register_debezium_connector.sh

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm dataflow-cli \
  bash deployments/docker/scripts/register_minio_sink_connector.sh

docker stop "${PRODUCER_NAME}" "${FLINK_NAME}" >/dev/null 2>&1 || true
docker rm -f "${PRODUCER_NAME}" "${FLINK_NAME}" >/dev/null 2>&1 || true

"${SCRIPT_DIR}/dataflow_compose.sh" run -d --name "${PRODUCER_NAME}" dataflow-cli \
  python data_generator/scripts/run_realtime_postgres_producer.py \
    --interval-seconds "${INTERVAL_SECONDS}" \
    --events-per-tick "${EVENTS_PER_TICK}"

"${SCRIPT_DIR}/dataflow_compose.sh" run -d --name "${FLINK_NAME}" flink-taskmanager \
  bash -lc "python3 -m pipelines.data_pipeline.feature_engineering.flink.realtime_stream_job --topic cdc.behavior_events --continuous --min-events 0"

cat <<EOF
Realtime continuous mode is running.

Producer container:
  ${PRODUCER_NAME}

Streaming container:
  ${FLINK_NAME}

Useful checks:
  docker logs -f ${PRODUCER_NAME}
  docker logs -f ${FLINK_NAME}
  make dataflow-smoke DATAFLOW_SMOKE_PHASE=bronze
  make dataflow-smoke DATAFLOW_SMOKE_PHASE=redis

Stop it with:
  make dataflow-realtime-down
EOF
