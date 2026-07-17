# Data Governance

DataHub governs the three rubric batch pipelines as `DP1`, `DP2`, and `DP3`. CDC and continuous feature processing are intentionally separated into `CDC_INGESTION` and `STREAMING_FEATURES`, so their lineage no longer changes the rubric numbering.

The governed flows are:

- `DP1`: Data Generator batch ingestion -> Bronze Parquet lakehouse.
- `DP2`: Bronze Parquet -> PySpark -> curated Silver Iceberg.
- `DP3`: Silver Iceberg -> PySpark features -> Iceberg feature tables -> PostgreSQL Feast offline store.
- `CDC_INGESTION`: source PostgreSQL -> Debezium -> `cdc.*` Kafka topics.
- `STREAMING_FEATURES`: `cdc.behavior_events` -> two continuously running Flink jobs -> configured Iceberg/PostgreSQL offline features and Redis online features.

Each rubric flow has an `ingest_stage` followed by a `validate_stage`. The validation stage writes a run-scoped JSON report and `latest.json` under `s3a://recsys-lakehouse/governance/validation/<pipeline>/`. DataHub reads that report and maps runtime results to `SUCCESS`, `FAILURE`, or `ERROR`; it never reports unconditional success.

Lineage is no longer declared as input/output tuples in the DataHub catalog. Each running PyArrow, Spark, Debezium, or Flink job emits OpenLineage-compatible `START`, `COMPLETE`, or `FAIL` events under `s3a://recsys-lakehouse/governance/lineage/`. The event contains the Airflow run ID, deterministic runtime UUID, event time, upstream jobs, and the datasets observed by that execution. DataHub clears the old direct dataset edges and rebuilds `DataJobInputOutput` exclusively from the latest runtime events.

Before DataHub ingestion, `verify_governance_coverage` fails the Airflow run unless every governed job has a runtime event, every one of the 51 datasets appears in runtime lineage, and every dataset has a schema, contract description, validation pipeline, and validation result. This prevents an incomplete but visually plausible lineage graph from being published.

Common code:

- [runtime_lineage.py (line 102)](../../../apps/data-platform/src/metadata/runtime_lineage.py#L102), [runtime_lineage.py (line 188)](../../../apps/data-platform/src/metadata/runtime_lineage.py#L188), [runtime_lineage.py (line 201)](../../../apps/data-platform/src/metadata/runtime_lineage.py#L201), [runtime_lineage.py (line 235)](../../../apps/data-platform/src/metadata/runtime_lineage.py#L235): builds, writes, and records run-scoped OpenLineage events from actual executions.
- [ingest_datahub_governance.py (line 869)](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L869), [ingest_datahub_governance.py (line 996)](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L996): loads runtime events, verifies complete coverage, and emits only runtime-observed job lineage.
- [ingest_datahub_governance.py (line 430)](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L430), [ingest_datahub_governance.py (line 515)](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L515): maps the latest runtime validation report to DataHub assertion results.
- [ingest_datahub_governance.py (line 372)](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L372), [ingest_datahub_governance.py (line 429)](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L429), [ingest_datahub_governance.py (line 517)](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L517), [ingest_datahub_governance.py (line 556)](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py#L556): attaches native schema and data-quality assertions to an active Data Contract.
- [governance_contracts.py (line 23)](../../../apps/data-platform/src/validate/governance_contracts.py#L23), [governance_contracts.py (line 99)](../../../apps/data-platform/src/validate/governance_contracts.py#L99): writes and reads the shared validation-report format.

## DP1 Linked With Related Tables

`recsys_dp1_raw_to_bronze` runs the Data Generator inside the batch task and ingests its ephemeral output directly into Bronze Parquet lakehouse tables. There is no separate MinIO data-lake stage or raw-S3 dataset in the governed lineage. MinIO is only the S3-compatible object-storage backend underneath the lakehouse. `validate_stage` checks table readability, `row_count > 0`, source key columns, `source_run_id`, and `lakehouse_ingestion_ts`.

### DP1 Lineage Image Proof

![DataHub lineage from the DP1 batch-ingestion jobs through Bronze tables into the downstream DP2 flow](../../pngs/dp1_lineage.png)

**Figure 1 — DP1 batch ingestion and downstream handoff.** DataHub shows the DP1 `Ingest Stage - Data Generator Batch Ingestion` and `Validate Stage`, the ten governed `recsys.lakehouse.bronze_*` Parquet outputs, and their downstream consumption by DP2. No raw-S3 dataset appears between the DP1 task and Bronze, confirming that DP1 writes directly to the governed Bronze lakehouse layer. The DP2 nodes on the right are downstream context, not DP1-owned outputs.

### DP1 Validation And Data Contract Image Proof

![Passing DP1 Data Contract for bronze_behavior_events with schema columns and data-quality assertion](../../pngs/dp1_datahub_contract.png)

**Figure 2 — DP1 schema and data-quality contract.** The `recsys.lakehouse.bronze_behavior_events` contract is passing, its Columns badge reports 30 fields, and the Schema table exposes field names and normalized types. The green data-quality assertion, `DP1` Data Product association, and `DataContract`/`NativePipeline` tags demonstrate that the Bronze table is governed by both structural and runtime-quality checks.

### Code Reference

- [batch_lakehouse_ingestion.py (line 85)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L85), [batch_lakehouse_ingestion.py (line 138)](../../../apps/data-platform/src/ingest/batch_lakehouse_ingestion.py#L138): DP1 Bronze enrichment, output, and lineage.
- [governance_contracts.py (line 102)](../../../apps/data-platform/src/validate/governance_contracts.py#L102), [governance_contracts.py (line 133)](../../../apps/data-platform/src/validate/governance_contracts.py#L133): `validate_dp1_bronze()` and report publication.
- [rubric_data_pipeline_dags.py (line 158)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L158), [rubric_data_pipeline_dags.py (line 216)](../../../apps/data-platform/src/orchestration/airflow/dags/rubric_data_pipeline_dags.py#L216): DP1 commands and `ingest_stage >> validate_stage` orchestration.

## DP2 Linked With Related Tables

`recsys_dp2_bronze_to_silver_gold` reads the DP1 Bronze Parquet tables and writes nine curated `silver_*` Iceberg tables. `clean_behavior_events` is normalized and deduplicated by `event_id`; rejected duplicate rows are kept in `silver_rejected_behavior_events` and may legitimately be empty.

### DP2 Lineage Image Proof

![Expanded DataHub lineage from DP1 Bronze tables through the DP2 PySpark jobs to Silver Iceberg tables](../../pngs/dp2_datahub_lineage.png)

**Figure 3 — DP2 Bronze-to-Silver transformation.** The expanded graph centers the DP2 `Ingest Stage` and `Validate Stage`, with ten DP1 Bronze inputs on the left and nine curated `iceberg.recsys.lakehouse.silver_*` outputs on the right. The additional DP3 feature nodes are downstream impact context; the DP2 evidence is the Bronze → PySpark tasks → Silver path.

### DP2 Validation And Data Contract Image Proof

![Passing DP2 Data Contract for silver_clean_behavior_events with 31 schema fields](../../pngs/dp2_datahub_contract.png)

**Figure 4 — DP2 curated-table contract.** The `iceberg.recsys.lakehouse.silver_clean_behavior_events` dataset is associated with DP2 and has a passing active contract. Its 31-column schema is rendered alongside the successful data-quality assertion, proving that the curated Silver output has both registered structure and runtime validation.

### Code Reference

- [dp2_silver_gold_entrypoint.py (line 20)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L20), [dp2_silver_gold_entrypoint.py (line 75)](../../../apps/data-platform/src/features/spark/dp2_silver_gold_entrypoint.py#L75): DP2 runtime lineage plus Silver validation/report publication.
- [build_silver_tables.py (line 14)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L14), [build_silver_tables.py (line 111)](../../../apps/data-platform/src/features/spark/build_silver_tables.py#L111): normalization, deduplication, rejected rows, and curated table construction.

## DP3 Linked With Related Tables

`recsys_dp3_offline_feature_table` now consumes DP2 `silver_*` tables directly. It does not rebuild Silver. PySpark computes five Iceberg feature outputs, exports the four Feast source tables to PostgreSQL, and validates both storage layers.

### DP3 Lineage Image Proof

![DataHub lineage from nine Silver Iceberg inputs through DP3 to Iceberg features and PostgreSQL Feast tables](../../pngs/dp3_datahub_lineage.png)

**Figure 5 — DP3 Silver-to-Feast offline-feature lineage.** Nine DP2 Silver datasets feed the DP3 `Ingest Stage`; the flow produces five Iceberg feature tables and exports the four Feast source tables to PostgreSQL before `Validate Stage`. The separate Iceberg and PostgreSQL nodes make the storage boundary explicit: Iceberg holds batch feature outputs, while PostgreSQL is the Feast offline store.

### DP3 Validation And Data Contract Image Proof

![Passing DP3 Data Contract for the PostgreSQL Feast ml_ranking_labels table](../../pngs/dp3_datahub_contract.png)

**Figure 6 — DP3 PostgreSQL Feast-table contract.** The final `postgres.feature_store.ml_ranking_labels` dataset is attached to DP3 and its active contract is passing. DataHub renders all 15 schema fields and a successful data-quality assertion, proving that governance continues across the Iceberg-to-PostgreSQL export boundary.

### Code Reference

- [spark_batch_entrypoint.py (line 39)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L39), [spark_batch_entrypoint.py (line 207)](../../../apps/data-platform/src/features/spark/spark_batch_entrypoint.py#L207): DP3 Silver inputs, Iceberg/PostgreSQL outputs, and validation report.
- [governance_contracts.py (line 134)](../../../apps/data-platform/src/validate/governance_contracts.py#L134), [governance_contracts.py (line 203)](../../../apps/data-platform/src/validate/governance_contracts.py#L203): `validate_dp3_postgres()` for Feast offline-store contracts.

## CDC Ingestion

`recsys_cdc_postgres_to_kafka` owns source PostgreSQL and Kafka datasets. The graph is `source_postgres.public.* -> Register Debezium Connector -> cdc.*`; it is no longer labelled DP1.

![DataHub lineage from ten source PostgreSQL tables through the Debezium connector task to ten CDC Kafka topics](../../pngs/cdc_datahub_lineage.png)

**Figure 7 — CDC ingestion lineage.** Ten source PostgreSQL tables feed the `Register Debezium Connector` task and map to ten `cdc.*` Kafka topics. The connector is represented as the processing node between the source tables and topics, and the dedicated `CDC PostgreSQL To Kafka` flow keeps this real-time ingestion path separate from rubric DP1.

Code reference: [register_k8s_connectors.py (line 28)](../../../apps/data-platform/src/ingest/register_k8s_connectors.py#L28), [register_k8s_connectors.py (line 91)](../../../apps/data-platform/src/ingest/register_k8s_connectors.py#L91). The accepted connector configuration determines the runtime source-table and Kafka-topic observations.

## Streaming Features

`recsys_flink_stream_features` contains two distinct jobs:

- `Run Flink Stream To Offline Store`: `cdc.behavior_events` -> the Iceberg or PostgreSQL sink enabled for that execution.
- `Run Flink Stream To Online Store`: `cdc.behavior_events` -> Redis feature keys.

The PostgreSQL datasets remain owned by DP3 and are only referenced by the streaming flow. This avoids duplicate Data Product ownership while retaining cross-flow lineage.

![DataHub lineage from cdc.behavior_events into separate Flink online-store and offline-store jobs](../../pngs/streaming_datahub_lineage.png)

**Figure 8 — Streaming feature-store processing.** The `cdc.behavior_events` topic branches into distinct `Run Flink Stream To Online Store` and `Run Flink Stream To Offline Store` jobs. The expanded offline branch shows the three PostgreSQL feature tables; the Redis children of the online-store job are collapsed in this capture. The two job nodes still make the online and offline processing responsibilities explicit.

Code reference: [realtime_stream_job.py (line 1182)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1182), [realtime_stream_job.py (line 1190)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1190), [realtime_stream_job.py (line 1200)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1200), [realtime_stream_job.py (line 1202)](../../../apps/data-platform/src/features/flink/realtime_stream_job.py#L1202). The event reports PostgreSQL or Iceberg offline outputs according to the sink actually enabled, and reports Redis outputs only when the online sink is enabled.

## Runtime Governance Verification

After the DP1, DP2, DP3, CDC, and streaming validation tasks have run, verify coverage without contacting DataHub:

```bash
python -m metadata.ingest_datahub_governance --verify-only
```

A successful result contains `"verified": true`, `"datasets": 51`, the latest status/run ID for every job, and a validation report for every data product. The full Kubernetes DAG runs this gate immediately before strict DataHub ingestion.
