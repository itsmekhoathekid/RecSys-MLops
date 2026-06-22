#!/usr/bin/env bash
set -euo pipefail

PRODUCER_NAME="${DATAFLOW_REALTIME_PRODUCER_NAME:-recsys-dataflow-realtime-producer}"
FLINK_NAME="${DATAFLOW_REALTIME_FLINK_NAME:-recsys-dataflow-realtime-flink}"

docker stop "${PRODUCER_NAME}" "${FLINK_NAME}" >/dev/null 2>&1 || true
docker rm -f "${PRODUCER_NAME}" "${FLINK_NAME}" >/dev/null 2>&1 || true

echo "Realtime continuous containers stopped: ${PRODUCER_NAME}, ${FLINK_NAME}"
