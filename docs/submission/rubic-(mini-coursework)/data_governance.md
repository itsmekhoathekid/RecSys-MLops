# Data Governance

DataHub governs the three rubric batch pipelines as `DP1`, `DP2`, and `DP3`. CDC and continuous feature processing are intentionally separated into `CDC_INGESTION` and `STREAMING_FEATURES`, so their lineage no longer changes the rubric numbering.

The governed flows are:

- `DP1`: Data Generator batch ingestion -> Bronze Parquet lakehouse.
- `DP2`: Bronze Parquet -> PySpark -> curated Silver Iceberg.
- `DP3`: Silver Iceberg -> PySpark features -> Iceberg feature tables -> PostgreSQL Feast offline store.
- `CDC_INGESTION`: source PostgreSQL -> Debezium -> `cdc.*` Kafka topics.
- `STREAMING_FEATURES`: `cdc.behavior_events` -> two continuously running Flink jobs -> PostgreSQL offline features and Redis online features.

Each rubric flow has an `ingest_stage` followed by a `validate_stage`. The validation stage writes a run-scoped JSON report and `latest.json` under `s3a://recsys-lakehouse/governance/validation/<pipeline>/`. DataHub reads that report and maps runtime results to `SUCCESS`, `FAILURE`, or `ERROR`; it never reports unconditional success.

Common code:

- [apps/data-platform/src/metadata/ingest_datahub_governance.py line 320](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L320): always UPSERTs dataset schema and lineage, including an empty upstream list that clears stale edges.
- [apps/data-platform/src/metadata/ingest_datahub_governance.py line 436](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L436): maps the latest runtime validation report to a DataHub assertion result.
- [apps/data-platform/src/metadata/ingest_datahub_governance.py line 523](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L523): attaches native schema and data-quality assertions to an active Data Contract.
- [apps/data-platform/src/validate/governance_contracts.py line 1](../../../apps/data-platform/src/validate/governance_contracts.py#L1): writes and reads the shared validation-report format.

## DP1 Linked With Related Tables

`recsys_dp1_raw_to_bronze` runs the Data Generator inside the batch task and ingests its ephemeral output directly into Bronze Parquet lakehouse tables. There is no separate MinIO data-lake stage or raw-S3 dataset in the governed lineage. MinIO is only the S3-compatible object-storage backend underneath the lakehouse. `validate_stage` checks table readability, `row_count > 0`, source key columns, `source_run_id`, and `lakehouse_ingestion_ts`.

### DP1 Lineage Image Proof

![DataHub lineage from the DP1 batch-ingestion jobs through Bronze tables into the downstream DP2 flow](../../pngs/dp1_lineage.png)

**Figure 1 — DP1 batch ingestion and downstream handoff.** DataHub shows the DP1 `Ingest Stage - Data Generator Batch Ingestion` and `Validate Stage`, the ten governed `recsys.lakehouse.bronze_*` Parquet outputs, and their downstream consumption by DP2. No raw-S3 dataset appears between the DP1 task and Bronze, confirming that DP1 writes directly to the governed Bronze lakehouse layer. The DP2 nodes on the right are downstream context, not DP1-owned outputs.

### DP1 Validation And Data Contract Image Proof

![Passing DP1 Data Contract for bronze_behavior_events with schema columns and data-quality assertion](../../pngs/dp1_datahub_contract.png)

**Figure 2 — DP1 schema and data-quality contract.** The `recsys.lakehouse.bronze_behavior_events` contract is passing, its Columns badge reports 30 fields, and the Schema table exposes field names and normalized types. The green data-quality assertion, `DP1` Data Product association, and `DataContract`/`NativePipeline` tags demonstrate that the Bronze table is governed by both structural and runtime-quality checks.

### Code Reference

- [apps/data-platform/src/metadata/ingest_datahub_governance.py line 637](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L637): DP1 datasets, jobs, and exact lineage.
- [apps/data-platform/src/validate/governance_contracts.py line 101](../../../apps/data-platform/src/validate/governance_contracts.py#L101): Bronze runtime validation checks.
- [apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py line 199](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L199): Airflow `ingest_stage -> validate_stage` dependency.

## DP2 Linked With Related Tables

`recsys_dp2_bronze_to_silver_gold` reads the DP1 Bronze Parquet tables and writes nine curated `silver_*` Iceberg tables. `clean_behavior_events` is normalized and deduplicated by `event_id`; rejected duplicate rows are kept in `silver_rejected_behavior_events` and may legitimately be empty.

### DP2 Lineage Image Proof

![Expanded DataHub lineage from DP1 Bronze tables through the DP2 PySpark jobs to Silver Iceberg tables](../../pngs/dp2_datahub_lineage.png)

**Figure 3 — DP2 Bronze-to-Silver transformation.** The expanded graph centers the DP2 `Ingest Stage` and `Validate Stage`, with ten DP1 Bronze inputs on the left and nine curated `iceberg.recsys.lakehouse.silver_*` outputs on the right. The additional DP3 feature nodes are downstream impact context; the DP2 evidence is the Bronze → PySpark tasks → Silver path.

### DP2 Validation And Data Contract Image Proof

![Passing DP2 Data Contract for silver_clean_behavior_events with 31 schema fields](../../pngs/dp2_datahub_contract.png)

**Figure 4 — DP2 curated-table contract.** The `iceberg.recsys.lakehouse.silver_clean_behavior_events` dataset is associated with DP2 and has a passing active contract. Its 31-column schema is rendered alongside the successful data-quality assertion, proving that the curated Silver output has both registered structure and runtime validation.

### Code Reference

- [apps/data-platform/src/metadata/ingest_datahub_governance.py line 684](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L684): exact Bronze inputs and Silver ownership/lineage.
- [apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py line 31](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L31): Silver validation and report publication.
- [apps/data-platform/src/features/spark/build_silver_tables.py line 95](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L95): normalization, deduplication, and Iceberg writes.

## DP3 Linked With Related Tables

`recsys_dp3_offline_feature_table` now consumes DP2 `silver_*` tables directly. It does not rebuild Silver. PySpark computes five Iceberg feature outputs, exports the four Feast source tables to PostgreSQL, and validates both storage layers.

### DP3 Lineage Image Proof

![DataHub lineage from nine Silver Iceberg inputs through DP3 to Iceberg features and PostgreSQL Feast tables](../../pngs/dp3_datahub_lineage.png)

**Figure 5 — DP3 Silver-to-Feast offline-feature lineage.** Nine DP2 Silver datasets feed the DP3 `Ingest Stage`; the flow produces five Iceberg feature tables and exports the four Feast source tables to PostgreSQL before `Validate Stage`. The separate Iceberg and PostgreSQL nodes make the storage boundary explicit: Iceberg holds batch feature outputs, while PostgreSQL is the Feast offline store.

### DP3 Validation And Data Contract Image Proof

![Passing DP3 Data Contract for the PostgreSQL Feast ml_ranking_labels table](../../pngs/dp3_datahub_contract.png)

**Figure 6 — DP3 PostgreSQL Feast-table contract.** The final `postgres.feature_store.ml_ranking_labels` dataset is attached to DP3 and its active contract is passing. DataHub renders all 15 schema fields and a successful data-quality assertion, proving that governance continues across the Iceberg-to-PostgreSQL export boundary.

### Code Reference

- [apps/data-platform/src/metadata/ingest_datahub_governance.py line 743](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L743): DP3 Silver inputs, Iceberg intermediates, and PostgreSQL outputs.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 177](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L177): reads existing DP2 Silver tables rather than rebuilding them.
- [apps/data-platform/src/features/spark/spark_batch_entrypoint.py line 114](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L114): publishes DP3 Iceberg validation observations.
- [apps/data-platform/src/validate/governance_contracts.py line 122](../../../apps/data-platform/src/validate/governance_contracts.py#L122): validates PostgreSQL Feast offline tables.

## CDC Ingestion

`recsys_cdc_postgres_to_kafka` owns source PostgreSQL and Kafka datasets. The graph is `source_postgres.public.* -> Register Debezium Connector -> cdc.*`; it is no longer labelled DP1.

![DataHub lineage from ten source PostgreSQL tables through the Debezium connector task to ten CDC Kafka topics](../../pngs/cdc_datahub_lineage.png)

**Figure 7 — CDC ingestion lineage.** Ten source PostgreSQL tables feed the `Register Debezium Connector` task and map to ten `cdc.*` Kafka topics. The connector is represented as the processing node between the source tables and topics, and the dedicated `CDC PostgreSQL To Kafka` flow keeps this real-time ingestion path separate from rubric DP1.

Code reference: [apps/data-platform/src/metadata/ingest_datahub_governance.py line 825](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L825).

## Streaming Features

`recsys_flink_stream_features` contains two distinct jobs:

- `Run Flink Stream To Offline Store`: `cdc.behavior_events` -> PostgreSQL Feast tables.
- `Run Flink Stream To Online Store`: `cdc.behavior_events` -> Redis feature keys.

The PostgreSQL datasets remain owned by DP3 and are only referenced by the streaming flow. This avoids duplicate Data Product ownership while retaining cross-flow lineage.

![DataHub lineage from cdc.behavior_events into separate Flink online-store and offline-store jobs](../../pngs/streaming_datahub_lineage.png)

**Figure 8 — Streaming feature-store processing.** The `cdc.behavior_events` topic branches into distinct `Run Flink Stream To Online Store` and `Run Flink Stream To Offline Store` jobs. The expanded offline branch shows the three PostgreSQL feature tables; the Redis children of the online-store job are collapsed in this capture. The two job nodes still make the online and offline processing responsibilities explicit.

Code reference: [apps/data-platform/src/metadata/ingest_datahub_governance.py line 871](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L871).
