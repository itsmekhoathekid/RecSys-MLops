#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
VERIFY_JOB="recsys-feature-store-verify"
SPARK_IMAGE="${DATA_PLATFORM_SPARK_IMAGE:-recsys-spark:local}"
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
          image: ${SPARK_IMAGE}
          imagePullPolicy: IfNotPresent
          envFrom:
            - configMapRef:
                name: recsys-data-platform-config
          command: ["/bin/bash", "-lc"]
          args:
            - |
              cd /opt/recsys
              export PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys
              cat >/tmp/verify_stream_feature_store.py <<'PY'
              import json
              from feature_engineering.spark.session import spark_session
              from lakehouse.iceberg import IcebergCatalogConfig

              spark = spark_session("recsys-verify-stream-feature-store")
              catalog = IcebergCatalogConfig()
              tables = [
                  "stream_behavior_events",
                  "stream_user_sequence_features",
                  "stream_user_aggregate_features",
                  "stream_item_features",
                  "streaming_quality_windows",
              ]
              counts = {}
              try:
                  for table in tables:
                      counts[table] = spark.table(catalog.feature_table(table)).count()
              finally:
                  spark.stop()
              missing = {table: count for table, count in counts.items() if count <= 0}
              if missing:
                  raise SystemExit(f"Iceberg stream feature tables are empty: {missing}; counts={counts}")
              print(json.dumps(counts, sort_keys=True))
              PY
              /opt/spark/bin/spark-submit /tmp/verify_stream_feature_store.py
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
