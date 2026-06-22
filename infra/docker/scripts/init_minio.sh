#!/bin/sh
set -eu

mc alias set local "${MINIO_ENDPOINT:-http://minio:9000}" "${MINIO_ROOT_USER:-minio}" "${MINIO_ROOT_PASSWORD:-minio123}"

mc mb -p "local/${LAKE_BUCKET:-recsys-lake}" || true
mc mb -p "local/${FEATURE_STORE_BUCKET:-recsys-feature-store}" || true

touch /tmp/.keep
mc cp /tmp/.keep "local/${LAKE_BUCKET:-recsys-lake}/raw/.keep"
mc cp /tmp/.keep "local/${LAKE_BUCKET:-recsys-lake}/bronze/.keep"
mc cp /tmp/.keep "local/${LAKE_BUCKET:-recsys-lake}/silver/.keep"
mc cp /tmp/.keep "local/${FEATURE_STORE_BUCKET:-recsys-feature-store}/offline/.keep"

mc ls "local/${LAKE_BUCKET:-recsys-lake}"
mc ls "local/${FEATURE_STORE_BUCKET:-recsys-feature-store}"

