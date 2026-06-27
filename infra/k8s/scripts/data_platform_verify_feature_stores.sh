#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
VERIFY_JOB="recsys-feature-store-verify"
SPARK_IMAGE="${DATA_PLATFORM_SPARK_IMAGE:-recsys-spark:local}"
VERIFY_IMAGE="${DATA_PLATFORM_VERIFY_IMAGE:-${SPARK_IMAGE}}"
SECRET_NAME="${DATA_PLATFORM_SECRET_NAME:-recsys-data-platform-secret}"
TIMEOUT_SECONDS="${DATA_PLATFORM_VERIFY_TIMEOUT_SECONDS:-240}"
SLEEP_SECONDS="${DATA_PLATFORM_VERIFY_SLEEP_SECONDS:-10}"

wait_for_command() {
  local description="$1"
  local output_file="$2"
  shift 2
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  while true; do
    if "$@" >"${output_file}" 2>/tmp/recsys-verify-command-error.log; then
      return 0
    fi
    if [[ "${SECONDS}" -ge "${deadline}" ]]; then
      echo "Timed out waiting for ${description}" >&2
      cat /tmp/recsys-verify-command-error.log >&2 || true
      return 1
    fi
    sleep "${SLEEP_SECONDS}"
  done
}

wait_for_command "Debezium connector status" /tmp/recsys-debezium-status.json \
  kubectl exec -n "${NAMESPACE}" deploy/kafka-connect -- \
  curl -fsS http://localhost:8083/connectors/recsys-postgres-cdc/status

wait_for_command "Flink REST job overview" /tmp/recsys-flink-jobs.json \
  kubectl exec -n "${NAMESPACE}" deploy/flink-jobmanager -- \
  curl -fsS http://localhost:8081/jobs/overview

python3 - <<'PY'
import json
from pathlib import Path

debezium = json.loads(Path("/tmp/recsys-debezium-status.json").read_text())
if debezium.get("connector", {}).get("state") != "RUNNING":
    raise SystemExit(f"Debezium connector is not RUNNING: {debezium}")
print({"debezium": "RUNNING"})
PY

flink_deadline=$((SECONDS + TIMEOUT_SECONDS))
while true; do
  if python3 - <<'PY'
import json
from pathlib import Path

flink = json.loads(Path("/tmp/recsys-flink-jobs.json").read_text())
running = [job for job in flink.get("jobs", []) if job.get("state") == "RUNNING"]
if not running:
    raise SystemExit(f"No RUNNING Flink jobs found: {flink}")
print({"flink_running_jobs": len(running)})
PY
  then
    break
  fi
  if [[ "${SECONDS}" -ge "${flink_deadline}" ]]; then
    echo "Timed out waiting for a RUNNING Flink job" >&2
    cat /tmp/recsys-flink-jobs.json >&2 || true
    exit 1
  fi
  sleep "${SLEEP_SECONDS}"
  wait_for_command "Flink REST job overview" /tmp/recsys-flink-jobs.json \
    kubectl exec -n "${NAMESPACE}" deploy/flink-jobmanager -- \
    curl -fsS http://localhost:8081/jobs/overview
done

deadline=$((SECONDS + TIMEOUT_SECONDS))
while true; do
  redis_keys="$(
    kubectl exec -n "${NAMESPACE}" deploy/redis -- \
      sh -lc "redis-cli --scan --pattern 'fs:user_sequence:*' | head -20 | wc -l | tr -d ' '"
  )"
  if [[ "${redis_keys}" -gt 0 ]]; then
    break
  fi
  if [[ "${SECONDS}" -ge "${deadline}" ]]; then
    echo "Timed out waiting for Redis online feature keys" >&2
    exit 1
  fi
  sleep "${SLEEP_SECONDS}"
done

kubectl delete job "${VERIFY_JOB}" -n "${NAMESPACE}" --ignore-not-found >/dev/null
cat <<YAML | kubectl apply -n "${NAMESPACE}" -f - >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${VERIFY_JOB}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: verify
          image: ${VERIFY_IMAGE}
          imagePullPolicy: IfNotPresent
          envFrom:
            - configMapRef:
                name: recsys-data-platform-config
            - secretRef:
                name: ${SECRET_NAME}
          command: ["/bin/bash", "-lc"]
          args:
            - |
              cd /opt/recsys
              cat >/tmp/verify_stream_feature_store.py <<'PY'
              import json
              import os

              import boto3


              tables = [
                  "stream_behavior_events",
                  "stream_user_sequence_features",
                  "stream_user_aggregate_features",
                  "stream_item_features",
                  "streaming_quality_windows",
              ]
              bucket = os.environ.get("OFFLINE_FEATURE_BUCKET", "recsys-offline-feature-store")
              namespace = os.environ.get("ICEBERG_FEATURE_NAMESPACE", "feature_store").strip("/")
              warehouse = os.environ.get("OFFLINE_FEATURE_STORE_WAREHOUSE", "s3a://recsys-offline-feature-store/warehouse")
              warehouse_prefix = warehouse.split(f"{bucket}/", 1)[-1].strip("/")
              endpoint = os.environ.get("MINIO_ENDPOINT") or os.environ.get("DATA_PLATFORM_MINIO_ENDPOINT")
              client = boto3.client(
                  "s3",
                  endpoint_url=endpoint,
                  aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("MINIO_ROOT_USER"),
                  aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("MINIO_ROOT_PASSWORD"),
                  region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
              )

              def count_prefix(prefix: str, suffix: str = "") -> int:
                  paginator = client.get_paginator("list_objects_v2")
                  count = 0
                  for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                      for item in page.get("Contents", []):
                          if not suffix or item["Key"].endswith(suffix):
                              count += 1
                              if count:
                                  return count
                  return count

              checks = {}
              for table in tables:
                  root = f"{warehouse_prefix}/{namespace}/{table}".strip("/")
                  checks[table] = {
                      "metadata_files": count_prefix(f"{root}/metadata/"),
                      "data_files": count_prefix(f"{root}/data/", ".parquet"),
                  }
              missing = {
                  table: result
                  for table, result in checks.items()
                  if result["metadata_files"] <= 0 or result["data_files"] <= 0
              }
              if missing:
                  raise SystemExit(f"Iceberg stream feature table files are missing: {missing}; checks={checks}")
              print(json.dumps(checks, sort_keys=True))
              PY
              python /tmp/verify_stream_feature_store.py
YAML

if ! kubectl wait -n "${NAMESPACE}" --for=condition=complete "job/${VERIFY_JOB}" --timeout="${TIMEOUT_SECONDS}s"; then
  kubectl logs -n "${NAMESPACE}" "job/${VERIFY_JOB}" || true
  kubectl delete job "${VERIFY_JOB}" -n "${NAMESPACE}" --ignore-not-found >/dev/null
  exit 1
fi

kubectl logs -n "${NAMESPACE}" "job/${VERIFY_JOB}"
kubectl delete job "${VERIFY_JOB}" -n "${NAMESPACE}" --ignore-not-found >/dev/null

echo "Redis online feature keys detected: ${redis_keys}"
echo "Streaming feature stores verified."
