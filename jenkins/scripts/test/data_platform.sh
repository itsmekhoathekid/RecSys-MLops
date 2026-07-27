#!/usr/bin/env bash

test_data_platform_base() {
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  component_test_wait_deployment "${namespace}" airflow-webserver
  component_test_wait_deployment "${namespace}" airflow-scheduler
  kubectl exec -n "${namespace}" deploy/airflow-webserver -c airflow-webserver -- \
    airflow db check
}

test_materialize() {
  test_data_platform_base
  component_test_airflow_dag recsys_feast_materialize
}

test_dp1() {
  test_data_platform_base
  component_test_wait_deployment "${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}" kafka-connect
  component_test_airflow_dag recsys_dp1_raw_to_bronze
}

test_dp2() {
  test_data_platform_base
  component_test_airflow_dag recsys_dp2_bronze_to_silver_gold
}

test_dp3() {
  test_data_platform_base
  component_test_airflow_dag recsys_dp3_offline_feature_table
}

test_stream_transaction_event() {
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  local event_id="ci-stream-${TX_ID:-${BUILD_NUMBER:-manual}}"
  local deadline=$((SECONDS + ${COMPONENT_TEST_TIMEOUT_SECONDS:-600}))
  local status=0
  local offline_count=""
  local redis_payload=""

  kubectl exec -n "${namespace}" deploy/realtime-event-producer \
    -c realtime-event-producer -- \
    env CI_STREAM_EVENT_ID="${event_id}" \
      PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys \
      /opt/venv/bin/python -c '
import os
from datetime import datetime, timezone

import psycopg

from streaming.event_factory import StreamEventFactory
from streaming.postgres import bootstrap_dimensions, conninfo, write_bundle

event_id = os.environ["CI_STREAM_EVENT_ID"]
now = datetime.now(timezone.utc)
rows = StreamEventFactory(1, 1).create(0, now, now)
rows["sessions"]["session_id"] = f"{event_id}-session"
rows["recommendation_requests"].update(
    request_id=f"{event_id}-request",
    session_id=f"{event_id}-session",
)
rows["impressions"].update(
    impression_id=f"{event_id}-impression",
    request_id=f"{event_id}-request",
    session_id=f"{event_id}-session",
)
rows["behavior_events"].update(
    event_id=event_id,
    payload_hash=event_id,
    session_id=f"{event_id}-session",
    request_id=f"{event_id}-request",
    impression_id=f"{event_id}-impression",
)
with psycopg.connect(conninfo()) as connection:
    with connection.cursor() as cursor:
        bootstrap_dimensions(cursor, now, 1, 1)
        write_bundle(cursor, rows)
    connection.commit()
    # A second CDC update with the same event_id exercises Flink idempotency.
    with connection.cursor() as cursor:
        write_bundle(cursor, rows)
    connection.commit()
'

  while ((SECONDS < deadline)); do
    offline_count="$(
      kubectl exec -n "${namespace}" deploy/airflow-webserver \
        -c airflow-webserver -- \
        env CI_STREAM_EVENT_ID="${event_id}" python -c '
import os
import psycopg

with psycopg.connect(
    host=os.environ.get("FEAST_POSTGRES_HOST", "feature-postgres"),
    port=int(os.environ.get("FEAST_POSTGRES_PORT", "5432")),
    dbname=os.environ.get("FEAST_POSTGRES_DB", "feature_store"),
    user=os.environ.get("FEAST_POSTGRES_USER", "feast"),
    password=os.environ.get("FEAST_POSTGRES_PASSWORD", "feast"),
) as connection:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM feature_store.user_sequence_features "
            "WHERE source_event_id = %s",
            (os.environ["CI_STREAM_EVENT_ID"],),
        )
        print(cursor.fetchone()[0])
' 2>/dev/null | tail -n 1
    )"
    redis_payload="$(
      kubectl exec -n "${namespace}" deploy/redis -- \
        redis-cli GET fs:user_sequence:900000 2>/dev/null || true
    )"
    if [[ "${offline_count}" == "1" && -n "${redis_payload}" ]]; then
      break
    fi
    sleep 10
  done

  if [[ "${offline_count}" != "1" ]]; then
    recsys_error "synthetic stream event was not written exactly once to the offline store: ${event_id} count=${offline_count:-missing}"
    status=1
  fi
  if [[ -z "${redis_payload}" ]]; then
    recsys_error "online feature was not visible in Redis for synthetic stream event: ${event_id}"
    status=1
  fi

  kubectl exec -n "${namespace}" deploy/airflow-webserver \
    -c airflow-webserver -- \
    env CI_STREAM_EVENT_ID="${event_id}" python -c '
import os
import psycopg

with psycopg.connect(
    host=os.environ.get("FEAST_POSTGRES_HOST", "feature-postgres"),
    port=int(os.environ.get("FEAST_POSTGRES_PORT", "5432")),
    dbname=os.environ.get("FEAST_POSTGRES_DB", "feature_store"),
    user=os.environ.get("FEAST_POSTGRES_USER", "feast"),
    password=os.environ.get("FEAST_POSTGRES_PASSWORD", "feast"),
) as connection:
    with connection.cursor() as cursor:
        for table in ("user_sequence_features", "user_aggregate_features", "item_features"):
            cursor.execute(
                f"DELETE FROM feature_store.{table} WHERE source_event_id = %s",
                (os.environ["CI_STREAM_EVENT_ID"],),
            )
    connection.commit()
' >/dev/null 2>&1 || true
  kubectl exec -n "${namespace}" deploy/realtime-event-producer \
    -c realtime-event-producer -- \
    env CI_STREAM_EVENT_ID="${event_id}" \
      PYTHONPATH=/opt/recsys/apps/data-platform/data-generator/src:/opt/recsys/apps/data-platform/src:/opt/recsys \
      /opt/venv/bin/python -c '
import os
import psycopg
from streaming.postgres import conninfo

event_id = os.environ["CI_STREAM_EVENT_ID"]
with psycopg.connect(conninfo()) as connection:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM behavior_events WHERE event_id = %s", (event_id,))
        cursor.execute("DELETE FROM impressions WHERE impression_id = %s", (f"{event_id}-impression",))
        cursor.execute("DELETE FROM recommendation_requests WHERE request_id = %s", (f"{event_id}-request",))
        cursor.execute("DELETE FROM sessions WHERE session_id = %s", (f"{event_id}-session",))
    connection.commit()
' >/dev/null 2>&1 || true

  return "${status}"
}

test_stream_features() {
  local status=0
  test_stream_transaction_event || status=$?
  DATA_PLATFORM_VERIFY_TIMEOUT_SECONDS="${COMPONENT_TEST_TIMEOUT_SECONDS:-600}" \
    infra/k8s/scripts/data_platform_verify_feature_stores.sh || status=$?
  return "${status}"
}

test_drift() {
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  local synthetic_id="ci-drift-${TX_ID:-${BUILD_NUMBER:-manual}}"
  test_data_platform_base
  kubectl exec -n "${namespace}" deploy/airflow-scheduler -c airflow-scheduler -- \
    bash -lc "
      set -euo pipefail
      report=/tmp/${synthetic_id}.json
      python -c 'import json; json.dump({\"run_id\":\"${synthetic_id}\",\"passed\":False,\"features\":[{\"feature_view\":\"ci\",\"feature\":\"synthetic\",\"passed\":False}]}, open(\"'\${report}'\", \"w\"))'
      PYTHONPATH=/opt/recsys/apps/data-platform/src \
        python -m mlops.trigger_kubeflow_retrain \
          --drift-report-path \"\${report}\" \
          --disable-retrain \
        | tee /tmp/${synthetic_id}-result.json
      python -c 'import json; p=json.load(open(\"/tmp/${synthetic_id}-result.json\")); assert p[\"reason\"] == \"retrain_disabled\"; assert p[\"triggered\"] is False'
      rm -f \"\${report}\" /tmp/${synthetic_id}-result.json
    "
}
