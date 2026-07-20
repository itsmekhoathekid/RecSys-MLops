# Data Pipeline Orchestration

The data platform exposes the three rubric data pipelines as Airflow DAGs. DP1 and DP2 use the optimized lakehouse sequence `ingest_stage -> optimize_stage -> validate_stage`; DP3 keeps the two-stage sequence `ingest_stage -> validate_stage`.

| Data product | Airflow DAG | Ordered flow |
| --- | --- | --- |
| DP1 | `recsys_dp1_raw_to_bronze` | Data Generator -> Bronze Iceberg ingest -> optimize -> validate |
| DP2 | `recsys_dp2_bronze_to_silver_gold` | Bronze Iceberg -> Silver Iceberg ingest -> optimize -> validate |
| DP3 | `recsys_dp3_offline_feature_table` | Silver Iceberg -> feature tables/PostgreSQL -> validate |

The DAGs reuse shared platform configuration through Kubernetes `ConfigMap` and `Secret` injection instead of hard-coding connection details in each task. The common `KubernetesPodOperator` wrapper loads `recsys-data-platform-config` and `recsys-data-platform-secret`, forwards logs to Airflow, disables sidecar injection for batch pods, and pins the task to the configured dataflow node pool.

Code references: shared environment injection is defined at [rubric_data_pipeline_dags.py (line 65)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L65), the common `KubernetesPodOperator` wrapper at [rubric_data_pipeline_dags.py (line 86)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L86), and the Spark submission wrapper at [rubric_data_pipeline_dags.py (line 105)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L105). The three DAG definitions start at [rubric_data_pipeline_dags.py (line 222)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L222), [rubric_data_pipeline_dags.py (line 248)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L248), and [rubric_data_pipeline_dags.py (line 274)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L274).

Storage terminology in this document is intentionally strict: Bronze Iceberg, Silver Iceberg, and intermediate Iceberg feature tables are zones or tables inside the same data lakehouse. PostgreSQL is the Feast offline feature store. The generator's temporary Parquet fragments are pod-local staging files, not a governed Parquet zone or an independent data lake.

## Pipeline To Ingest Raw Data Into Bronze Zone (DP1)

DP1 is the direct batch ingestion pipeline from the Data Generator to the Iceberg Bronze zone. The generator writes temporary Parquet fragments inside the Airflow batch pod; the same `ingest_stage` immediately commits them into Bronze Iceberg tables, and the temporary pod output disappears when the task finishes. There is no separate governed Parquet layer between the generator and Iceberg.

Flow: `Data Generator -> ingest_stage -> Bronze Iceberg -> optimize_stage -> validate_stage`.

Input: historical data generated inside the batch pod from `$DATA_GENERATOR_CONFIG`.

Output: `recsys.lakehouse.bronze_<table>` Iceberg tables under `$LAKEHOUSE_WAREHOUSE`.

DataHub identifies the physical tables through the logical URNs `recsys.lakehouse.bronze_<table>`; lineage represents the Iceberg datasets rather than their S3-compatible storage backend.

### Reference Code

- DP1 ingest, optimize, and validate commands: [rubric_data_pipeline_dags.py (line 166)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L166), [rubric_data_pipeline_dags.py (line 182)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L182), and [rubric_data_pipeline_dags.py (line 192)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L192).
- `recsys_dp1_raw_to_bronze` DAG and its ordered dependency: [rubric_data_pipeline_dags.py (line 222)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L222) and [rubric_data_pipeline_dags.py (line 246)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L246).
- Config-driven historical generation inside the DP1 pod: [cli.py (line 34)](../../../apps/data-platform/data-generator/src/cli.py#L34).
- `load_generator_run_to_lakehouse()` entry point: [batch_lakehouse_ingestion.py (line 69)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L69).
- Run metadata enrichment and Bronze Iceberg write: [batch_lakehouse_ingestion.py (line 91)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L91) and [batch_lakehouse_ingestion.py (line 97)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L97).

### Stage Explanation

`ingest_stage` runs two steps in one Airflow task. First, it calls the historical Data Generator with `$DATA_GENERATOR_CONFIG`; output remains ephemeral inside that Kubernetes pod. Second, it runs `batch_lakehouse_ingestion.py` and atomically writes every generated table to Bronze Iceberg with `source_run_id` and `lakehouse_ingestion_ts` metadata.

`optimize_stage` runs the shared lakehouse optimizer with `--scope bronze --pipeline DP1`, applying compaction, target file sizing, compression, manifest maintenance, and the configured optimization strategy.

`validate_stage` reads the configured Bronze namespace through the Spark Iceberg catalog. It verifies every table is readable and non-empty, contains its source key plus `source_run_id` and `lakehouse_ingestion_ts`, and has no null values in those required fields.

### Image Proof Show Ingest Stage & Validate Stage

![DP1 Airflow DAG proof](../../pngs/dp1_airflow_ui.png)

**Figure: DP1 Airflow orchestration proof.** The captured Airflow Graph shows the original ingest/validate path for DAG `recsys_dp1_raw_to_bronze`. The current DAG preserves those endpoints and inserts `optimize_stage` between them; the source reference above is authoritative for the current three-stage graph.

## Pipeline To Ingest Data From Bronze Into Silver Zone (DP2)

DP2 is the Spark batch processing pipeline from DP1 Bronze Iceberg tables to curated Silver Iceberg tables. It handles timestamp normalization, compatible schema evolution, duplicate behavior-event rejection, order fact construction, product slowly changing dimension preparation, and curated `silver_*` writes.

Flow: `Bronze Iceberg -> ingest_stage -> Silver Iceberg -> optimize_stage -> validate_stage`.

Input: DP1 Bronze Iceberg tables.

Output: curated `silver_*` Apache Iceberg lakehouse tables such as clean behavior events, rejected behavior events, clean impressions, clean recommendation requests, order facts, product SCD, users, products, and user preferences.

The historical DAG, function, and entrypoint identifiers retain the `silver_gold` suffix. These are runtime identifiers only; DP2 does not write a physical Gold layer.

### Reference Code

- DP2 ingest, validate, and optimize commands: [rubric_data_pipeline_dags.py (line 198)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L198), [rubric_data_pipeline_dags.py (line 204)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L204), and [rubric_data_pipeline_dags.py (line 210)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L210).
- `recsys_dp2_bronze_to_silver_gold` DAG and its ordered dependency: [rubric_data_pipeline_dags.py (line 248)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L248) and [rubric_data_pipeline_dags.py (line 272)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L272).
- `build_dp2_silver_gold()` and `validate_dp2_silver_gold()` entry points: [dp2_silver_gold_entrypoint.py (line 15)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L15) and [dp2_silver_gold_entrypoint.py (line 29)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L29).
- Timestamp/schema normalization and `.dropDuplicates(["event_id"])`: [build_silver_tables.py (line 28)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L28) and [build_silver_tables.py (line 45)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L45).
- Unsupported-schema rejection, order facts, and product SCD output: [build_silver_tables.py (line 41)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L41), [build_silver_tables.py (line 66)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L66), and [build_silver_tables.py (line 75)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L75).
- Curated `silver_*` Iceberg writes: [build_silver_tables.py (line 117)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L117).

### Stage Explanation

`ingest_stage` submits a Spark-on-Kubernetes job that runs `dp2_silver_gold_entrypoint.py --action ingest`. The Spark job reads DP1 Bronze Iceberg tables, normalizes event timestamps, adds missing schema-evolution columns when needed, applies `.dropDuplicates(["event_id"])` to supported behavior events, quarantines unsupported schemas, and commits the curated `silver_*` Iceberg tables.

`optimize_stage` runs the shared optimizer with `--scope silver --pipeline DP2` before validation. `validate_stage` then submits the same Spark entrypoint with `--action validate`, checks every `silver_*` Iceberg table, permits the rejected-event table to be empty, validates required event columns, and requires `duplicate_event_id = 0` in `silver_clean_behavior_events`.

### Image Proof Show Ingest Stage & Validate Stage

![DP2 Airflow DAG proof](../../pngs/dp2_airflow_ui.png)

**Figure: DP2 Airflow orchestration proof.** The captured Airflow Graph shows the original ingest/validate path. The current DAG preserves both tasks and inserts `optimize_stage` between them, producing `ingest_stage -> optimize_stage -> validate_stage`.

## Pipeline To Compute Feature Tables And Populate The Offline Feature Store (DP3)

DP3 is the Spark batch feature-engineering pipeline from curated Silver Iceberg tables to model-ready feature tables. It consumes DP2 curated data, computes model features, writes feature outputs to the Iceberg feature lakehouse, and exports the serving/training feature tables into PostgreSQL. These feature outputs form the ML-oriented, Gold-like serving layer, but the repository does not use a physical `gold_*` namespace. PostgreSQL is the Feast offline feature store, while Apache Iceberg remains the lakehouse storage layer. DP3 is therefore the bridge between the data platform and the ML system: it produces user sequence features, user aggregate features, item features, and labels/training samples where configured.

Flow: `Silver Iceberg -> PySpark feature engineering -> Iceberg feature tables -> PostgreSQL Feast offline store`.

Input: curated `silver_*` Apache Iceberg lakehouse tables from DP2 and the Spark batch feature config.

Output: offline feature tables in the feature lakehouse path plus PostgreSQL tables used by Feast as the offline feature store.

### Reference Code

- DP3 Spark command and PostgreSQL validation command: [rubric_data_pipeline_dags.py (line 157)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L157) and [rubric_data_pipeline_dags.py (line 163)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L163).
- `recsys_dp3_offline_feature_table` DAG and its stage dependency: [rubric_data_pipeline_dags.py (line 274)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L274) and [rubric_data_pipeline_dags.py (line 293)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L293).
- `run_pyspark_batch()` entry point and direct DP2 Silver read: [spark_batch_entrypoint.py (line 152)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L152) and [spark_batch_entrypoint.py (line 181)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L181).
- Feature computation and Iceberg writes: [spark_batch_entrypoint.py (line 186)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L186) and [spark_batch_entrypoint.py (line 188)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L188).
- PostgreSQL export and DP3 Iceberg validation report: [spark_batch_entrypoint.py (line 197)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L197) and [spark_batch_entrypoint.py (line 198)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L198).
- Feast PostgreSQL table validation entry point: [governance_contracts.py (line 148)](../../../apps/data-platform/src/validate/governance_contracts.py#L148).
- Required-column, row-count, and non-null key/timestamp checks: [governance_contracts.py (line 178)](../../../apps/data-platform/src/validate/governance_contracts.py#L178), [governance_contracts.py (line 180)](../../../apps/data-platform/src/validate/governance_contracts.py#L180), and [governance_contracts.py (line 189)](../../../apps/data-platform/src/validate/governance_contracts.py#L189).

### Stage Explanation

`ingest_stage` submits the production Spark batch feature job through `spark-submit`. The job reads the existing DP2 `silver_*` Iceberg tables directly; it does not rebuild Silver. It computes user sequence, user aggregate, item, ranking-label, and training feature outputs, writes Iceberg feature tables, then exports the four Feast source tables to PostgreSQL.

`validate_stage` connects to the PostgreSQL Feast offline store and checks table existence, required columns, row counts, and non-null entity keys/timestamps. It merges those observations with the Iceberg checks emitted by `ingest_stage`, producing one DP3 report that covers both intermediate feature tables and the real Feast offline store.

### Image Proof Show Ingest Stage & Validate Stage

![DP3 Airflow DAG proof](../../pngs/dp3_airflow_ui.png)

**Figure: DP3 Airflow orchestration proof.** The Airflow Graph tab shows DAG `recsys_dp3_offline_feature_table` with `ingest_stage` followed by `validate_stage`. Both nodes are green and labeled `success`, proving the Spark feature computation stage completed and the PostgreSQL offline-store validation stage passed in the same Airflow run.
