#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PRODUCER_NAME="${DATAFLOW_REALTIME_PRODUCER_NAME:-recsys-dataflow-realtime-producer}"
FLINK_NAME="${DATAFLOW_REALTIME_FLINK_NAME:-recsys-dataflow-realtime-flink}"
INTERVAL_SECONDS="${DATAFLOW_REALTIME_INTERVAL_SECONDS:-2}"
EVENTS_PER_TICK="${DATAFLOW_REALTIME_EVENTS_PER_TICK:-5}"

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm dataflow-cli \
  bash -lc "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys python infra/docker/scripts/init_postgres_schema.py"

"${SCRIPT_DIR}/dataflow_compose.sh" run --rm dataflow-cli \
  bash infra/docker/scripts/register_debezium_connector.sh

docker stop "${PRODUCER_NAME}" "${FLINK_NAME}" >/dev/null 2>&1 || true
docker rm -f "${PRODUCER_NAME}" "${FLINK_NAME}" >/dev/null 2>&1 || true

"${SCRIPT_DIR}/dataflow_compose.sh" run -d --name "${PRODUCER_NAME}" dataflow-cli \
  bash -lc "PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys python apps/data-platform/data-generator/src/scripts/run_realtime_postgres_producer.py \
    --interval-seconds '${INTERVAL_SECONDS}' \
    --events-per-tick '${EVENTS_PER_TICK}'"

"${SCRIPT_DIR}/dataflow_compose.sh" run -d --name "${FLINK_NAME}" flink-taskmanager \
  bash -lc "PYTHONPATH=/opt/flink/opt/python:/opt/recsys/apps/data-platform/src:/opt/recsys flink run -m flink-jobmanager:8081 -py apps/data-platform/src/features/flink/realtime_stream_job.py -- --runner pyflink --topic cdc.behavior_events --continuous --min-events 0 --offline-store-enabled --offline-feature-catalog \"\$OFFLINE_FEATURE_CATALOG\" --offline-feature-store-warehouse \"\$OFFLINE_FEATURE_STORE_WAREHOUSE\""

cat <<EOF
Realtime continuous mode is running.

Producer container:
  ${PRODUCER_NAME}

Streaming container:
  ${FLINK_NAME}

Useful checks:
  docker logs -f ${PRODUCER_NAME}
  docker logs -f ${FLINK_NAME}
  docker exec recsys-dataflow-redis-1 redis-cli DBSIZE

Stop it with:
  make dataflow-realtime-down
EOF
