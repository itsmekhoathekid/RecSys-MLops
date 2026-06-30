# Data Governance

This document covers the rubric rows:

- DP1, DP2, and DP3 are linked with related tables.
- Lineage exists between pipeline jobs and tables.
- Data validation and data contract are documented.
- DataHub UI proof shows lineage, validation, and data contracts.

## Governance Implementation

Code reference:

- [apps/data-platform/src/metadata/ingest_datahub_governance.py](../../../apps/data-platform/src/metadata/ingest_datahub_governance.py): emits DP1/DP2/DP3 data products, datasets, jobs, tags, lineage, and contract metadata to DataHub.
- [apps/data-platform/src/ingest/postgres_cdc_contracts.py](../../../apps/data-platform/src/ingest/postgres_cdc_contracts.py): DP1 source table contract.
- [apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py](../../../apps/data-platform/src/orchestration/airflow/dags/k8s_data_platform_dag.py): `datahub_ingest` Airflow task.
- [infra/helm/recsys-observability/dashboards/datahub-governance.json](../../../infra/helm/recsys-observability/dashboards/datahub-governance.json): DataHub ingest/runtime dashboard.

## DP1 Governance

| Asset | DataHub entity | Contract/validation |
|---|---|---|
| Source Postgres tables | `source_postgres.public.<table>` | `SourceTableContract` primary key and Debezium topic mapping. |
| Kafka CDC topics | `cdc.<table>` | Debezium envelope keyed by source table primary key. |
| Job | `register_debezium_connector` | Links Postgres upstreams to Kafka topic outputs. |

## DP2 Governance

| Asset | DataHub entity | Contract/validation |
|---|---|---|
| `stream_behavior_events` | Iceberg feature-store dataset | CDC behavior events persisted from Flink. |
| `stream_user_sequence_features` | Iceberg feature-store dataset | User event sequence table contract. |
| `stream_user_aggregate_features` | Iceberg feature-store dataset | User aggregate table contract. |
| `stream_item_features` | Iceberg feature-store dataset | Item aggregate table contract. |
| Job | `run_flink_stream_to_feature_stores` | Kafka CDC input to Iceberg feature-store outputs. |

## DP3 Governance

| Asset | DataHub entity | Contract/validation |
|---|---|---|
| Raw lakehouse tables | `parquet.recsys_lakehouse.lakehouse.<table>` | Generator raw table contract. |
| Offline feature tables | `iceberg.recsys_features.feature_store.<table>` | Iceberg offline feature contract. |
| Redis online feature keys | `redis_online.<feature>` | `RedisOnlineWriter` key/payload contract. |
| Jobs | `ingest_historical_batch_to_lakehouse`, `run_spark_batch_to_offline_store`, `run_flink_stream_to_online_store` | Links raw, offline, and online feature assets. |

## Run And Check Logs

```bash
cd /Users/KHOAI/anhkhoa/RecSys-MLops

kubectl get pods -n datahub
kubectl logs -n recsys-dataflow deploy/airflow-scheduler --tail=200 | rg 'datahub_ingest|DataHub|ingested'
kubectl port-forward -n datahub svc/datahub-datahub-frontend 9002:9002
```

Manual ingest command:

```bash
kubectl run -n recsys-dataflow datahub-ingest-proof \
  --rm -i --restart=Never \
  --image=asia-southeast1-docker.pkg.dev/fsds-coursework/recsys/recsys-dataflow-cli:gcp \
  --env=PYTHONPATH=/opt/recsys/apps/data-platform/src:/opt/recsys \
  -- python -m metadata.ingest_datahub_governance \
  --gms-url http://datahub-datahub-gms.datahub.svc.cluster.local:8080
```

Expected ingest summary:

```json
{
  "data_products": ["DP1", "DP2", "DP3"],
  "datasets": 35,
  "jobs": 5,
  "ingested": true
}
```

Observed GCP ingest result:

```text
DP1 -> urn:li:dataProduct:fa50010e-8abd-444a-ae59-046f473c5b50
DP2 -> urn:li:dataProduct:0117c171-99e6-4b6b-beb1-3059300e2ef8
DP3 -> urn:li:dataProduct:228cfd2c-388b-4313-9797-543305c02433
datasets: 35
jobs: 5
ingested: true
```

Image proof:

![DataHub GCP](../../pngs/datahub_gcp.png)
