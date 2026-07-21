# Data Pipeline Orchestration

This document covers the complete Airflow surface and the step-by-step execution of DP1, DP2, and DP3. DP1 and DP2 place lakehouse optimization inside the governed data-product run; DP3 remains a two-stage feature pipeline.

## Airflow Surface

Six operational DAGs are deployed. The three rubric data-product DAGs are defined in [rubric_data_pipeline_dags.py](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py); the Feast and drift/retrain DAGs are defined in [k8s_data_platform_dag.py](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py); the Superset-facing analytics flow is defined in [analytics_dag.py](../../../apps/analytics/orchestration/airflow/dags/analytics_dag.py).

| Purpose | DAG ID | Default schedule | Ordered stages |
|---|---|---|---|
| DP1 | `recsys_dp1_raw_to_bronze` | manual | `ingest_stage -> optimize_stage -> validate_stage` |
| DP2 | `recsys_dp2_bronze_to_silver_gold` | manual | `ingest_stage -> optimize_stage -> validate_stage` |
| DP3 | `recsys_dp3_offline_feature_table` | manual | `ingest_stage -> validate_stage` |
| Feast materialization | `recsys_feast_materialize` | every two hours | apply feature repo -> materialize -> validate Redis |
| Drift and conditional retraining | `recsys_feature_drift_monitoring` | daily | drift report -> metrics -> Kubeflow retrain trigger |
| Analytics and Superset marts | `recsys_analytics_daily` | daily | sync Silver -> build dbt Gold marts |

The obsolete composite `k8s_data_platform_dag` DAG ID, duplicate `recsys_batch_feature_pipeline`, raw-ingestion wrappers, and standalone `recsys_lakehouse_maintenance` DAG are not part of the Airflow surface. The file named `k8s_data_platform_dag.py` is retained only as the source module for the two operational Feast/drift DAGs above.

Code references:

- Schedule normalization: [rubric_data_pipeline_dags.py (line 58)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L58).
- DP1, DP2, and DP3 DAG definitions: [rubric_data_pipeline_dags.py (line 222)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L222), [line 248](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L248), and [line 274](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L274).
- Feast materialization and drift/retrain DAG definitions: [k8s_data_platform_dag.py (line 152)](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#L152) and [line 177](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py#L177).
- Analytics DAG definition and Silver-to-dbt dependency: [analytics_dag.py (line 47)](../../../apps/analytics/orchestration/airflow/dags/analytics_dag.py#L47) and [line 68](../../../apps/analytics/orchestration/airflow/dags/analytics_dag.py#L68).

## Kubernetes Execution Model

`pod_task()` creates a `KubernetesPodOperator` in `recsys-dataflow`. Every task receives the shared ConfigMap and Secret, runs with `set -euo pipefail`, disables the Istio sidecar for finite batch work, streams logs to Airflow, selects the configured node pool, and deletes its pod after completion. See [rubric_data_pipeline_dags.py (line 65)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L65) and [line 86](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L86).

`spark_native_submit()` submits Spark applications in Kubernetes cluster mode and forwards the lakehouse catalogs, object-store credentials, validation/lineage settings, resource limits, shuffle sizing, and dynamic-allocation settings to the driver and executors. `spark.kubernetes.submission.waitAppCompletion=true` prevents Airflow from marking a task successful before its Spark application finishes. See [rubric_data_pipeline_dags.py (line 105)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L105) and [line 127](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L127).

## DP1: Data Generator To Bronze Iceberg

### Airflow Stage Order

```text
Data Generator
  -> ingest_stage
  -> recsys.lakehouse.bronze_* Iceberg tables
  -> optimize_stage
  -> validate_stage
```

The generator's Parquet fragments exist only inside the DP1 task pod. They are an ephemeral exchange format, not a persistent governed zone. The first persistent DP1 datasets are the ten Bronze Iceberg tables.

### Step 1: Ingest Stage

`DP1_INGEST_COMMAND` performs two operations in one Airflow task:

1. Run the historical generator with `$DATA_GENERATOR_CONFIG`.
2. Start Spark locally in the same pod and load that run through `batch_lakehouse_ingestion.py`.
3. Read all ten generated tables with Parquet schema merge enabled.
4. Add `source_run_id` and `lakehouse_ingestion_ts`.
5. Create the Iceberg namespace when necessary.
6. atomically create or replace `recsys.lakehouse.bronze_<source_table>`.
7. Record the ten Bronze Iceberg URNs as runtime lineage outputs.

References: [rubric_data_pipeline_dags.py (line 166)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L166), [batch_lakehouse_ingestion.py (line 69)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L69), [line 87](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L87), and [line 97](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L97).

### Step 2: Optimize Stage

`DP1_OPTIMIZE_COMMAND` runs `optimize.py --scope bronze --pipeline DP1`. All ten Bronze tables receive compaction, target-file sizing, Zstandard compression, hash write distribution, manifest maintenance, and optional Z-order clustering. Missing tables fail the governed run because `--skip-missing` is not used.

References: [rubric_data_pipeline_dags.py (line 182)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L182), [optimize.py (line 34)](../../../apps/data-platform/src/lakehouse/optimize.py#L34), and [line 130](../../../apps/data-platform/src/lakehouse/optimize.py#L130).

### Step 3: Validate Stage

The validator reads every Bronze table through the Spark Iceberg catalog and requires a positive row count, all source primary-key/audit columns, and no nulls in those required fields. It publishes the DP1 governance report and records `optimize_stage` as its upstream job.

References: [rubric_data_pipeline_dags.py (line 192)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L192), [governance_contracts.py (line 102)](../../../apps/data-platform/src/validate/governance_contracts.py#L102), and [line 121](../../../apps/data-platform/src/validate/governance_contracts.py#L121).

### Airflow DAG And Image Proof

The DAG creates all three tasks and enforces their order at [rubric_data_pipeline_dags.py (line 222)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L222) and [line 246](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L246).

![DP1 successful Airflow DAG run](../../pngs/airflow_dp1_raw_to_bronze_success.png)

**Figure: DP1 Airflow orchestration proof.** The successful Graph run shows the current `ingest_stage -> optimize_stage -> validate_stage` dependency for `recsys_dp1_raw_to_bronze`; all three tasks are green.

## DP2: Bronze Iceberg To Silver Iceberg

### Airflow Stage Order

```text
Bronze Iceberg
  -> Spark ingest_stage
  -> Silver Iceberg
  -> optimize_stage
  -> Spark validate_stage
```

### Step 1: Ingest Stage

1. Submit `dp2_silver_gold_entrypoint.py --action ingest` through Spark on Kubernetes.
2. Read all ten DP1 tables through `catalog.bronze_table()` and the Iceberg catalog.
3. Normalize timestamps and compatible schema-evolution columns.
4. Separate unsupported schema versions.
5. Deduplicate supported behavior events by `event_id`.
6. Build clean events/impressions/requests, order facts, product SCD, users, products, and preferences.
7. Commit nine curated `silver_*` Iceberg tables and runtime lineage.

References: [rubric_data_pipeline_dags.py (line 198)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L198), [dp2_silver_gold_entrypoint.py (line 15)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L15), and [build_silver_tables.py (line 28)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L28).

### Step 2: Optimize Stage

`DP2_OPTIMIZE_COMMAND` runs the shared optimizer with `--scope silver --pipeline DP2`. All nine Silver tables receive the same physical-file policy as DP1; clean behavior events and impressions also have optional user/item/time Z-order profiles.

References: [rubric_data_pipeline_dags.py (line 210)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L210), [optimize.py (line 24)](../../../apps/data-platform/src/lakehouse/optimize.py#L24), and [line 38](../../../apps/data-platform/src/lakehouse/optimize.py#L38).

### Step 3: Validate Stage

The validation action opens all persisted Silver tables, requires normal outputs to be non-empty, permits only `silver_rejected_behavior_events` to be empty, verifies required event columns, and requires `duplicate_event_id = 0` for `silver_clean_behavior_events`.

References: [rubric_data_pipeline_dags.py (line 204)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L204) and [dp2_silver_gold_entrypoint.py (line 29)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L29).

### Airflow DAG And Image Proof

The DAG creates all three tasks and enforces their order at [rubric_data_pipeline_dags.py (line 248)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L248) and [line 272](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L272).

![DP2 successful Airflow DAG run](../../pngs/airflow_dp2_bronze_to_silver_success.png)

**Figure: DP2 Airflow orchestration proof.** The successful Graph run shows `ingest_stage -> optimize_stage -> validate_stage` for `recsys_dp2_bronze_to_silver_gold`; all three tasks are green.

## DP3: Feature Tables And Feast Offline Store

### Airflow Stage Order

```text
Silver Iceberg
  -> Spark ingest_stage
  -> Iceberg feature/training tables
  -> PostgreSQL Feast offline tables
  -> validate_stage
```

### Step 1: Ingest Stage

1. Submit `spark_batch_entrypoint.py` through Spark on Kubernetes.
2. Read the existing DP2 `silver_*` Iceberg tables; DP3 does not rebuild Silver.
3. Compute user-sequence, user-aggregate, item, ranking-label, and training outputs.
4. Commit the Iceberg feature tables.
5. Export the four Feast source tables to PostgreSQL.

References: [rubric_data_pipeline_dags.py (line 157)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L157), [spark_batch_entrypoint.py (line 152)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L152), [line 181](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L181), and [line 197](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L197).

### Step 2: Validate Stage

The PostgreSQL validator checks table existence, the configured schema, positive row counts, required entity/timestamp columns, and non-null key/timestamp values. It merges these observations with the Iceberg checks emitted during ingestion.

References: [rubric_data_pipeline_dags.py (line 163)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L163) and [governance_contracts.py (line 148)](../../../apps/data-platform/src/validate/governance_contracts.py#L148).

### Airflow DAG And Image Proof

The DAG and dependency are defined at [rubric_data_pipeline_dags.py (line 274)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L274) and [line 293](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L293).

![DP3 successful Airflow DAG run](../../pngs/airflow_dp3_offline_features_success.png)

**Figure: DP3 Airflow orchestration proof.** The successful Graph run shows `ingest_stage -> validate_stage` for `recsys_dp3_offline_feature_table`; both tasks are green.

## End-To-End Ordering

The deployment-level coordinator triggers the data-product DAGs serially:

```text
DP1 success -> DP2 success -> DP3 success -> feature-store verification
```

`cluster_data_setup.sh` reads the ordered DAG list, unpauses each DAG, creates a deterministic run ID, waits for terminal success, and stops immediately on failure or timeout. See [cluster_data_setup.sh (line 8)](../../../infra/k8s/scripts/cluster_data_setup.sh#L8) and [line 80](../../../infra/k8s/scripts/cluster_data_setup.sh#L80).

Run the complete data setup with:

```bash
make cluster-data-setup
```

## Run And Check Airflow

```bash
# List the six deployed DAGs.
kubectl exec -n recsys-dataflow deploy/airflow-webserver -- airflow dags list

# Trigger one data product manually.
kubectl exec -n recsys-dataflow deploy/airflow-webserver -- \
  airflow dags trigger recsys_dp1_raw_to_bronze

# Inspect recent runs and task state.
kubectl exec -n recsys-dataflow deploy/airflow-webserver -- \
  airflow dags list-runs -d recsys_dp1_raw_to_bronze
```

Task logs are streamed by `KubernetesPodOperator`; Spark tasks additionally wait for and report the cluster-mode application result.

## Failure Behavior

- Generator or Bronze commit failure stops DP1 before optimization.
- Missing tables or a failed Iceberg rewrite stop DP1/DP2 before validation.
- A contract failure writes its governance report, exits non-zero, and fails the validation task.
- The E2E coordinator never triggers DP2 before DP1 succeeds or DP3 before DP2 succeeds.
- `catchup=False` and `max_active_runs=1` prevent backfill storms and overlapping runs of the same DAG.
- Spark completion waiting prevents a submitted-but-failed driver from being reported as an Airflow success.
