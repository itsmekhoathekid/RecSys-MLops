#!/usr/bin/env bash

test_data_platform_base() {
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  component_test_wait_deployment "${namespace}" airflow-webserver
  component_test_wait_deployment "${namespace}" airflow-scheduler
  if kubectl get deployment/airflow-dag-processor -n "${namespace}" >/dev/null 2>&1; then
    component_test_wait_deployment "${namespace}" airflow-dag-processor
  fi
  kubectl exec -n "${namespace}" deploy/airflow-webserver -c airflow-webserver -- \
    airflow db check
}

test_materialize() {
  test_data_platform_base
  component_test_airflow_dag_registered recsys_feast_materialize
}

test_dp1() {
  test_data_platform_base
  component_test_wait_deployment "${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}" kafka-connect
  component_test_airflow_dag_registered recsys_dp1_raw_to_bronze
}

test_dp2() {
  test_data_platform_base
  component_test_airflow_dag_registered recsys_dp2_bronze_to_silver_gold
}

test_dp3() {
  test_data_platform_base
  component_test_airflow_dag_registered recsys_dp3_offline_feature_table
}

test_debezium_connector_tasks() {
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  kubectl exec -n "${namespace}" deploy/kafka-connect -- \
    curl -fsS http://localhost:8083/connectors/recsys-postgres-cdc/status \
    | python3 -c '
import json
import sys

status = json.load(sys.stdin)
connector_state = status.get("connector", {}).get("state")
task_states = [task.get("state") for task in status.get("tasks", [])]
if connector_state != "RUNNING" or not task_states or any(
    state != "RUNNING" for state in task_states
):
    raise SystemExit(
        "Debezium connector is unhealthy: "
        f"connector={connector_state}, tasks={task_states}"
    )
print({"connector": connector_state, "tasks": task_states})
'
}

test_stream_features() {
  local namespace="${DATA_PLATFORM_NAMESPACE:-recsys-dataflow}"
  test_debezium_connector_tasks
  component_test_wait_deployment "${namespace}" realtime-flink-offline-store
  component_test_wait_deployment "${namespace}" realtime-flink-online-store
  kubectl exec -n "${namespace}" deploy/flink-jobmanager -- \
    curl -fsS http://localhost:8081/jobs/overview \
    | python3 -c '
import json
import sys

jobs = json.load(sys.stdin).get("jobs", [])
running = [job for job in jobs if job.get("state") == "RUNNING"]
if len(running) < 2:
    raise SystemExit(f"Expected two RUNNING Flink jobs, found: {jobs}")
print({"flink_running_jobs": len(running)})
'
  kubectl exec -n "${namespace}" deploy/redis -- redis-cli PING \
    | grep -Fxq PONG
  kubectl exec -n "${namespace}" statefulset/feature-postgres -- \
    pg_isready -U feast -d feature_store
}

test_drift() {
  test_data_platform_base
  component_test_airflow_dag_registered recsys_feature_drift_monitoring
}
