# Data Pipeline Orchestration

## Airflow surface

The three data-product DAGs remain authoritative for DP1/DP2/DP3 and are defined in [rubric_data_pipeline_dags.py](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py):

| Data product | DAG ID | Schedule default | Ordered stages |
|---|---|---|---|
| DP1 | `recsys_dp1_raw_to_bronze` | manual | `ingest_stage -> optimize_stage -> validate_stage` |
| DP2 | `recsys_dp2_bronze_to_silver_gold` | manual | `ingest_stage -> optimize_stage -> validate_stage` |
| DP3 | `recsys_dp3_offline_feature_table` | manual | `ingest_stage -> validate_stage` |

The platform also deploys the operational DAGs required to run and visualize the complete system:

| DAG ID | Default schedule | Purpose |
|---|---|---|
| `k8s_data_platform_dag` | daily | Runs DP1 -> DP2 -> DP3 -> Feast -> analytics -> drift as a visible end-to-end graph. |
| `recsys_batch_feature_pipeline` | `0 1 * * *` | Rebuilds optimized Silver Iceberg data and the offline feature store. |
| `recsys_feast_materialize` | `20 */2 * * *` | Applies the Feast registry, materializes PostgreSQL offline features to Redis, and validates Redis. |
| `recsys_feature_drift_monitoring` | `30 3 * * *` | Computes offline feature drift, pushes metrics, and conditionally triggers Kubeflow retraining. |
| `recsys_analytics_daily` | `30 2 * * *` | Syncs Silver Iceberg data, builds dbt marts, and refreshes the data consumed by Apache Superset. |

`env_schedule()` converts `manual`, `none`, and an empty value to Airflow `schedule=None`. Every DAG has `catchup=False` and `max_active_runs=1`, so scheduled work cannot overlap an earlier run of the same product.

The obsolete raw-ingestion, local-only, streaming-wrapper, and standalone lakehouse-maintenance DAGs remain removed. The Airflow runtime image packages both the data-platform DAG directory and [analytics_dag.py](../../../apps/analytics/orchestration/airflow/dags/analytics_dag.py); this keeps the operational DAGs visible without restoring duplicate legacy wrappers.

## Kubernetes execution model

`pod_task()` creates a `KubernetesPodOperator` in the `recsys-dataflow` namespace. Each pod:

- receives the shared data-platform ConfigMap and Secret;
- runs with `set -euo pipefail`;
- disables Istio sidecar injection for finite batch work;
- streams logs back to Airflow;
- is deleted after completion;
- uses the configured CPU-services node selector.

`spark_native_submit()` submits DP1 optimization/validation, DP2 stages, and DP3 ingestion to Spark on Kubernetes in cluster mode. It forwards lakehouse catalogs, object-store credentials, validation/lineage settings, Spark memory, timeout, shuffle, and dynamic-allocation settings to both driver and executors.

The DP1 generator and initial Iceberg commit run with Spark local mode inside one Spark task pod. This is deliberate: the generator's Parquet fragments are ephemeral pod-local files and are never promoted to a persistent Parquet zone. The task immediately commits them to shared Bronze Iceberg tables before the pod is deleted.

## DP1: Data Generator to Bronze Iceberg

Flow:

```text
Data Generator
  -> ingest_stage
  -> recsys.lakehouse.bronze_* Iceberg tables
  -> optimize_stage
  -> validate_stage
```

### Ingest stage

The command in [rubric_data_pipeline_dags.py](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py) runs the historical generator using `$DATA_GENERATOR_CONFIG`, then executes [batch_lakehouse_ingestion.py](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py) with Spark.

The ingestion job:

1. reads all ten generator tables with schema merge enabled;
2. adds `source_run_id` and `lakehouse_ingestion_ts`;
3. creates the Iceberg namespace if necessary;
4. writes `recsys.lakehouse.bronze_<source_table>` with an atomic Iceberg create/replace commit;
5. records the ten Bronze Iceberg URNs as DP1 runtime lineage outputs.

There is no governed Parquet dataset between the generator and Iceberg.

### Optimize stage

[optimize.py](../../../apps/data-platform/src/lakehouse/optimize.py) runs with `--scope bronze --pipeline DP1`. It applies the shared compaction, write sizing, compression, optional Z-order, and manifest maintenance policy to all ten Bronze tables. It also emits runtime lineage with the Bronze tables as both inputs and outputs and `ingest_stage` as the upstream job.

### Validate stage

[governance_contracts.py](../../../apps/data-platform/src/validate/governance_contracts.py) reads each table through the Iceberg catalog. It requires:

- a positive row count;
- all source primary-key columns;
- `source_run_id` and `lakehouse_ingestion_ts`;
- no null values in those required fields.

The task publishes a DP1 validation report and records `optimize_stage` as its upstream runtime job.

## DP2: Bronze Iceberg to Silver Iceberg

Flow:

```text
Bronze Iceberg
  -> Spark ingest_stage
  -> Silver Iceberg
  -> optimize_stage
  -> Spark validate_stage
```

### Ingest stage

[dp2_silver_gold_entrypoint.py](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py) calls `build_silver_tables(..., source="lakehouse")`. [build_silver_tables.py](../../../apps/data-platform/src/features/spark/build_silver_tables.py) resolves each source with `catalog.bronze_table()` and reads it through Spark's Iceberg catalog.

The transformation normalizes timestamps and optional columns, separates unsupported schema versions, deduplicates behavior events/impressions, builds order facts and product history, then commits nine `silver_*` Iceberg tables.

### Optimize stage

The shared runner executes with `--scope silver --pipeline DP2`. All nine Silver tables receive the same optimization policy used by DP1; the two dominant event/impression access paths also have optional Z-order profiles.

### Validate stage

The DP2 validation action opens all persisted Silver tables, requires every normal output to be non-empty, allows only `silver_rejected_behavior_events` to be empty, and requires `duplicate_event_id=0` for `silver_clean_behavior_events`. Validation starts only after the optimization stage succeeds.

## DP3: Offline feature table

DP3 remains a two-stage flow because this change scopes mandatory lakehouse optimization to DP1 and DP2.

The ingest stage runs [spark_batch_entrypoint.py](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py), reads DP2 Silver Iceberg tables, builds batch feature/training tables, commits the feature Iceberg outputs, and exports the Feast offline tables to PostgreSQL. The validation stage runs `governance_contracts dp3-postgres` to verify required columns, row counts, and non-null entity/timestamp fields.

## End-to-end ordering

[cluster_data_setup.sh](../../../infra/k8s/scripts/cluster_data_setup.sh) is the deployment-level E2E coordinator. It triggers and waits for each DAG in this order:

```text
DP1 success -> DP2 success -> DP3 success -> feature-store verification
```

Each run has a deterministic prefix plus the DAG ID, and the script stops immediately if any Airflow run fails or times out. This preserves cross-DAG dependencies without reintroducing a fourth composite DAG.

Run it with:

```bash
make cluster-data-setup
```

## Failure behavior

- Generator or Iceberg commit failure fails DP1 `ingest_stage`; optimization never starts.
- Missing Bronze table or failed rewrite fails DP1 `optimize_stage`; validation never starts.
- Contract failure returns a non-zero exit code and fails the relevant validation task.
- DP2 cannot run as part of the E2E setup until DP1 succeeds.
- DP3 cannot run as part of the E2E setup until DP2 succeeds.
- Spark submission waits for application completion, so Airflow never marks a stage successful merely because the driver pod was created.
