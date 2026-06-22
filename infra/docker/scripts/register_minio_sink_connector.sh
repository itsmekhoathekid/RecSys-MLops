#!/bin/sh
set -eu

CONNECT_URL="${KAFKA_CONNECT_URL:-http://kafka-connect:8083}"
CONFIG_PATH="${1:-infra/docker/debezium/kafka-connect-s3-sink.json}"

curl -fsS -X PUT \
  -H "Content-Type: application/json" \
  --data "$(jq -c '.config' "$CONFIG_PATH")" \
  "$CONNECT_URL/connectors/$(jq -r '.name' "$CONFIG_PATH")/config"

echo

