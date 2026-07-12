# RecSys analytics

This module owns BI-facing analytical models. It does not replace the operational Hadoop Iceberg catalog used by the data platform.

## Flow

1. `sync_silver.py` reads curated `recsys.lakehouse.silver_*` tables and snapshots them into the isolated `analytics.staging` JDBC Iceberg catalog.
2. dbt on Trino builds `intermediate`, `core`, and `recsys` Gold schemas.
3. Superset receives a read-only Trino connection and should expose only `core` and `recsys` datasets.
4. `recsys_analytics_daily` runs the sync and dbt build once per day through Airflow.

The Helm release bootstraps the published `RecSys Business Pulse` dashboard idempotently. Its
Gold-only semantic datasets power KPI cards, a recommendation funnel, daily engagement,
conversion and revenue trends, category/brand rankings, and a product-performance explorer.

The JDBC catalog, Superset metadata database, and Redis cache are separate services. Production passwords are synchronized from the `analytics` source secret through the cluster `recsys-central-secrets` store into `recsys-analytics-secret`. Chart defaults are local-development placeholders only.

## Local validation

```bash
docker build -f apps/analytics/Dockerfile.dbt -t recsys-analytics-dbt:local .
docker run --rm recsys-analytics-dbt:local parse
helm lint infra/helm/recsys-analytics
```

To execute against a deployed stack, run the Airflow DAG or invoke `sync_silver.py`, then `dbt build`. A/B marts remain empty until recommendation request context includes both a real `experiment_id` and `variant`.
