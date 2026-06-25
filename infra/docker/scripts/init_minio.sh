#!/bin/sh
set -eu

mc alias set local "${MINIO_ENDPOINT:-http://minio:9000}" "${MINIO_ROOT_USER:-minio}" "${MINIO_ROOT_PASSWORD:-minio123}"

mc mb -p "local/${LAKE_BUCKET:-recsys-lakehouse}" || true
mc mb -p "local/${OFFLINE_FEATURE_BUCKET:-recsys-offline-feature-store}" || true

touch /tmp/.keep
mc cp /tmp/.keep "local/${LAKE_BUCKET:-recsys-lakehouse}/raw/.keep"
mc cp /tmp/.keep "local/${LAKE_BUCKET:-recsys-lakehouse}/warehouse/.keep"
mc cp /tmp/.keep "local/${OFFLINE_FEATURE_BUCKET:-recsys-offline-feature-store}/warehouse/.keep"

mc ls "local/${LAKE_BUCKET:-recsys-lakehouse}"
mc ls "local/${OFFLINE_FEATURE_BUCKET:-recsys-offline-feature-store}"
