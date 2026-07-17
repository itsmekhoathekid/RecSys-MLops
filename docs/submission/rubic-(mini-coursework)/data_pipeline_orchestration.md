# Data Pipeline Orchestration

The data platform exposes the three rubric data pipelines as Airflow DAGs. Each DAG follows the same two-stage structure:

1. `ingest_stage`: run the actual pipeline transformation.
2. `validate_stage`: evaluate the pipeline-specific runtime contract and fail the DAG when a required check fails.

The DAGs reuse shared platform configuration through Kubernetes `ConfigMap` and `Secret` injection instead of hard-coding connection details in each task. The common `KubernetesPodOperator` wrapper loads `recsys-data-platform-config` and `recsys-data-platform-secret`, forwards logs to Airflow, disables sidecar injection for batch pods, and pins the task to the configured dataflow node pool.

Code references: shared environment injection is defined at [rubric_data_pipeline_dags.py (line 65)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L65), the common `KubernetesPodOperator` wrapper at [rubric_data_pipeline_dags.py (line 86)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L86), and the Spark submission wrapper at [rubric_data_pipeline_dags.py (line 105)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L105). The three DAG definitions start at [rubric_data_pipeline_dags.py (line 197)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L197), [rubric_data_pipeline_dags.py (line 218)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L218), and [rubric_data_pipeline_dags.py (line 239)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L239).

Storage terminology in this document is intentionally strict: Bronze Parquet, Silver Iceberg, and intermediate Iceberg feature tables are zones or tables inside the same data lakehouse. PostgreSQL is the Feast offline feature store. The architecture does not introduce an independent data-lake layer between the Data Generator and the lakehouse.

## Pipeline To Ingest Raw Data Into Bronze Zone (DP1)

DP1 is the direct batch ingestion pipeline from the Data Generator to the Parquet lakehouse Bronze zone. The generator writes temporary files inside the Airflow batch pod; the same `ingest_stage` immediately loads them into Bronze and the temporary pod output disappears when the task finishes. There is no separate raw data-lake or object-storage stage in this flow. The object-storage implementation is only infrastructure beneath the governed lakehouse warehouse.

Flow: `Data Generator -> batch ingest_stage -> Bronze Parquet lakehouse`.

Input: historical data generated inside the batch pod from `$DATA_GENERATOR_CONFIG`.

Output: Bronze Parquet tables under `$LAKEHOUSE_WAREHOUSE/lakehouse/<table>`.

DataHub identifies these physical tables through logical Parquet URNs named `recsys.lakehouse.bronze_<table>`; the lineage therefore represents the lakehouse dataset rather than its S3-compatible storage backend.

### Reference Code

- `DP1_INGEST_COMMAND`: [rubric_data_pipeline_dags.py (line 158)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L158).
- `DP1_VALIDATE_COMMAND`: [rubric_data_pipeline_dags.py (line 170)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L170).
- `recsys_dp1_raw_to_bronze` DAG and its stage dependency: [rubric_data_pipeline_dags.py (line 197)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L197) and [rubric_data_pipeline_dags.py (line 215)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L215).
- Config-driven historical generation inside the DP1 pod: [cli.py (line 34)](../../../apps/data-platform/data-generator/src/cli.py#L34).
- `load_generator_run_to_lakehouse()` entry point: [batch_lakehouse_ingestion.py (line 129)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L129).
- Run metadata enrichment and Bronze Parquet write: [batch_lakehouse_ingestion.py (line 146)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L146) and [batch_lakehouse_ingestion.py (line 149)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L149).

### Stage Explanation

`ingest_stage` runs two steps in one Airflow task. First, it calls the historical Data Generator with `$DATA_GENERATOR_CONFIG`; output remains ephemeral inside that Kubernetes pod. Second, it runs `ingest.batch_lakehouse_ingestion` against the local run path and writes every generated table directly into the Bronze lakehouse warehouse with `source_run_id` and `lakehouse_ingestion_ts` metadata.

`validate_stage` reads the configured Bronze namespace with PyArrow. It verifies every table is readable and non-empty and contains its source key plus `source_run_id` and `lakehouse_ingestion_ts`. The observed values and Airflow run ID are written to the DP1 governance report.

### Image Proof Show Ingest Stage & Validate Stage

![DP1 Airflow DAG proof](../../pngs/dp1_airflow_ui.png)

**Figure: DP1 Airflow orchestration proof.** The Airflow Graph tab shows DAG `recsys_dp1_raw_to_bronze` with exactly two ordered tasks: `ingest_stage -> validate_stage`. The left task list shows both stages present in the same DAG, and the graph node labels confirm both tasks are executed by `KubernetesPodOperator`.

## Pipeline To Ingest Data From Bronze Into Silver And Gold Zone (DP2)

DP2 is the Spark batch processing pipeline from DP1 Bronze Parquet tables to curated Silver Iceberg tables. It handles timestamp normalization, compatible schema evolution, duplicate behavior-event rejection, order fact construction, product slowly changing dimension preparation, and curated `silver_*` writes.

Flow: `Bronze Parquet -> PySpark -> Silver Iceberg`.

Input: DP1 Bronze Parquet tables.

Output: curated silver/gold-style Apache Iceberg lakehouse tables such as clean behavior events, rejected behavior events, clean impressions, clean recommendation requests, order facts, product SCD, users, products, and user preferences.

### Reference Code

- DP2 Spark ingest and validation commands: [rubric_data_pipeline_dags.py (line 172)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L172) and [rubric_data_pipeline_dags.py (line 178)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L178).
- `recsys_dp2_bronze_to_silver_gold` DAG and its stage dependency: [rubric_data_pipeline_dags.py (line 218)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L218) and [rubric_data_pipeline_dags.py (line 236)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L236).
- `build_dp2_silver_gold()` and `validate_dp2_silver_gold()` entry points: [dp2_silver_gold_entrypoint.py (line 20)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L20) and [dp2_silver_gold_entrypoint.py (line 35)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L35).
- Timestamp/schema normalization and event deduplication: [build_silver_tables.py (line 29)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L29) and [build_silver_tables.py (line 46)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L46).
- Rejected duplicate rows, order facts, and product SCD output: [build_silver_tables.py (line 49)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L49), [build_silver_tables.py (line 73)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L73), and [build_silver_tables.py (line 82)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L82).
- Curated `silver_*` Iceberg writes: [build_silver_tables.py (line 124)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L124).

### Stage Explanation

`ingest_stage` submits a Spark-on-Kubernetes job that runs `dp2_silver_gold_entrypoint.py --action ingest`. The Spark job reads DP1 bronze Parquet tables, normalizes event timestamps, adds missing schema-evolution columns when needed, deduplicates behavior events by latest `ingestion_ts`, builds silver tables, and writes them back to the lakehouse as `silver_*` tables.

`validate_stage` submits the same Spark entrypoint with `--action validate`. It checks every `silver_*` Iceberg table, permits the rejected-event table to be empty, validates required event columns, and requires `duplicate_event_id = 0` in `silver_clean_behavior_events`. Results are published to the DP2 governance report before DP3 consumes Silver.

### Image Proof Show Ingest Stage & Validate Stage

![DP2 Airflow DAG proof](../../pngs/dp2_airflow_ui.png)

**Figure: DP2 Airflow orchestration proof.** The Airflow Graph tab shows DAG `recsys_dp2_bronze_to_silver_gold` with the required `ingest_stage -> validate_stage` order. The proof also shows recent green duration bars on the left for both stages, meaning the two-stage Spark pipeline ran successfully in Airflow.

## Pipeline To Compute Offline Feature Table (DP3)

DP3 is the Spark batch feature-engineering pipeline from curated silver/gold lakehouse tables to offline feature tables. It consumes DP2 curated data, computes model features, writes feature outputs to the feature lakehouse path, and exports the serving/training feature tables into PostgreSQL. In this project, PostgreSQL is the Feast offline feature store, while Apache Iceberg remains the data lakehouse/storage layer. DP3 is therefore the bridge between the data platform and the ML system: it produces user sequence features, user aggregate features, item features, and labels/training samples where configured.

Flow: `Apache Iceberg Silver/Gold -> feature lakehouse outputs -> PostgreSQL Feast offline store`.

Input: curated silver/gold Apache Iceberg lakehouse tables from DP2 and the Spark batch feature config.

Output: offline feature tables in the feature lakehouse path plus PostgreSQL tables used by Feast as the offline feature store.

### Reference Code

- DP3 Spark command and PostgreSQL validation command: [rubric_data_pipeline_dags.py (line 149)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L149) and [rubric_data_pipeline_dags.py (line 155)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L155).
- `recsys_dp3_offline_feature_table` DAG and its stage dependency: [rubric_data_pipeline_dags.py (line 239)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L239) and [rubric_data_pipeline_dags.py (line 257)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L257).
- `run_pyspark_batch()` entry point and direct DP2 Silver read: [spark_batch_entrypoint.py (line 152)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L152) and [spark_batch_entrypoint.py (line 181)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L181).
- Feature computation and Iceberg writes: [spark_batch_entrypoint.py (line 186)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L186) and [spark_batch_entrypoint.py (line 188)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L188).
- PostgreSQL export and DP3 Iceberg validation report: [spark_batch_entrypoint.py (line 197)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L197) and [spark_batch_entrypoint.py (line 198)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L198).
- Feast PostgreSQL table validation entry point: [governance_contracts.py (line 134)](../../../apps/data-platform/src/validate/governance_contracts.py#L134).
- Required-column, row-count, and non-null key/timestamp checks: [governance_contracts.py (line 164)](../../../apps/data-platform/src/validate/governance_contracts.py#L164), [governance_contracts.py (line 166)](../../../apps/data-platform/src/validate/governance_contracts.py#L166), and [governance_contracts.py (line 175)](../../../apps/data-platform/src/validate/governance_contracts.py#L175).

### Stage Explanation

`ingest_stage` submits the production Spark batch feature job through `spark-submit`. The job reads the existing DP2 `silver_*` Iceberg tables directly; it does not rebuild Silver. It computes user sequence, user aggregate, item, ranking-label, and training feature outputs, writes Iceberg feature tables, then exports the four Feast source tables to PostgreSQL.

`validate_stage` connects to the PostgreSQL Feast offline store and checks table existence, required columns, row counts, and non-null entity keys/timestamps. It merges those observations with the Iceberg checks emitted by `ingest_stage`, producing one DP3 report that covers both intermediate feature tables and the real Feast offline store.

### Image Proof Show Ingest Stage & Validate Stage

![DP3 Airflow DAG proof](../../pngs/dp3_airflow_ui.png)

**Figure: DP3 Airflow orchestration proof.** The Airflow Graph tab shows DAG `recsys_dp3_offline_feature_table` with `ingest_stage` followed by `validate_stage`. Both nodes are green and labeled `success`, proving the Spark feature computation stage completed and the PostgreSQL offline-store validation stage passed in the same Airflow run.
