#!/bin/sh
set -eu

echo "Waiting for Kafka Connect..."
until curl -fsS "${KAFKA_CONNECT_URL:-http://kafka-connect:8083}/connectors" >/dev/null; do
  sleep 2
done

echo "Waiting for MinIO..."
until curl -fsS "${MINIO_ENDPOINT:-http://minio:9000}/minio/health/live" >/dev/null; do
  sleep 2
done

echo "Services reachable."

