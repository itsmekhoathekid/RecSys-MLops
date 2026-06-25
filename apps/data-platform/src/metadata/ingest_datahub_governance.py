from __future__ import annotations

import argparse
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from monitoring.pushgateway import MetricSample, push_metrics


ACTOR = "urn:li:corpuser:datahub"
ENV = "PROD"
GOVERNANCE_DOMAIN_NAME = "RecSys Data Platform"
GOVERNANCE_DOMAIN_DESCRIPTION = "Governed data platform domain for RecSys DP1/DP2/DP3."


def dataset_urn(platform: str, name: str, env: str = ENV) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


def flow_urn(flow_id: str, cluster: str = ENV) -> str:
    return f"urn:li:dataFlow:(airflow,{flow_id},{cluster})"


def job_urn(flow: str, job_id: str) -> str:
    return f"urn:li:dataJob:({flow},{job_id})"


@dataclass(frozen=True)
class Dataset:
    urn: str
    name: str
    description: str
    tags: tuple[str, ...]
    custom_properties: dict[str, str]
    upstreams: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    description: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    tags: tuple[str, ...]
    custom_properties: dict[str, str]


@dataclass(frozen=True)
class DataProduct:
    id: str
    flow_id: str
    flow_name: str
    description: str
    tags: tuple[str, ...]
    datasets: tuple[Dataset, ...]
    jobs: tuple[Job, ...]


class DataHubEmitter:
    def __init__(self, gms_url: str) -> None:
        self.gms_url = gms_url.rstrip("/")

    def emit(self, entity_urn: str, entity_type: str, aspect_name: str, aspect: dict[str, Any]) -> None:
        payload = {
            "proposal": {
                "entityType": entity_type,
                "entityUrn": entity_urn,
                "changeType": "UPSERT",
                "aspectName": aspect_name,
                "aspect": {
                    "value": json.dumps(aspect, separators=(",", ":"), sort_keys=True),
                    "contentType": "application/json",
                },
            }
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.gms_url}/aspects?action=ingestProposal",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-RestLi-Protocol-Version": "2.0.0",
            },
            method="POST",
        )
        for attempt in range(1, 6):
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    if response.status >= 300:
                        raise RuntimeError(f"DataHub ingest failed with HTTP {response.status}")
                    return
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code < 500 or attempt == 5:
                    raise RuntimeError(f"DataHub ingest failed for {entity_urn} {aspect_name}: HTTP {exc.code} {detail}") from exc
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                if attempt == 5:
                    raise RuntimeError(f"DataHub ingest failed for {entity_urn} {aspect_name}: {exc}") from exc
            time.sleep(2 * attempt)

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.gms_url}/api/graphql",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(1, 6):
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    if body.get("errors"):
                        raise RuntimeError(json.dumps(body["errors"], sort_keys=True))
                    return body["data"]
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code < 500 or attempt == 5:
                    raise RuntimeError(f"DataHub GraphQL failed: HTTP {exc.code} {detail}") from exc
            except (TimeoutError, socket.timeout, urllib.error.URLError, RuntimeError) as exc:
                if attempt == 5:
                    raise RuntimeError(f"DataHub GraphQL failed: {exc}") from exc
            time.sleep(2 * attempt)


def audit_stamp() -> dict[str, Any]:
    return {"time": int(time.time() * 1000), "actor": ACTOR}


def tag_associations(tags: tuple[str, ...]) -> dict[str, Any]:
    return {"tags": [{"tag": f"urn:li:tag:{tag}"} for tag in tags]}


def emit_tag(emitter: DataHubEmitter, tag: str, description: str, color_hex: str) -> None:
    emitter.emit(
        f"urn:li:tag:{tag}",
        "tag",
        "tagProperties",
        {
            "name": tag,
            "description": description,
            "colorHex": color_hex,
        },
    )


def find_entity_by_exact_name(emitter: DataHubEmitter, entity_type: str, name: str) -> dict[str, Any] | None:
    data = emitter.graphql(
        """
        query searchEntity($input: SearchAcrossEntitiesInput!) {
          searchAcrossEntities(input: $input) {
            searchResults {
              entity {
                urn
                type
                ... on Domain {
                  properties { name description }
                }
                ... on DataProduct {
                  properties { name description }
                  domain { domain { urn properties { name } } }
                }
              }
            }
          }
        }
        """,
        {"input": {"types": [entity_type], "query": name, "start": 0, "count": 25}},
    )
    for result in data["searchAcrossEntities"]["searchResults"]:
        entity = result["entity"]
        if entity.get("properties", {}).get("name") == name:
            return entity
    return None


def find_data_product_in_domain(emitter: DataHubEmitter, product_id: str, domain_urn: str) -> dict[str, Any] | None:
    data = emitter.graphql(
        """
        query searchDataProduct($input: SearchAcrossEntitiesInput!) {
          searchAcrossEntities(input: $input) {
            searchResults {
              entity {
                urn
                type
                ... on DataProduct {
                  properties { name description }
                  domain { domain { urn properties { name } } }
                }
              }
            }
          }
        }
        """,
        {"input": {"types": ["DATA_PRODUCT"], "query": product_id, "start": 0, "count": 25}},
    )
    for result in data["searchAcrossEntities"]["searchResults"]:
        entity = result["entity"]
        if entity.get("properties", {}).get("name") == product_id and (entity.get("domain") or {}).get("domain", {}).get("urn") == domain_urn:
            return entity
    return None


def ensure_governance_domain(emitter: DataHubEmitter) -> str:
    existing = find_entity_by_exact_name(emitter, "DOMAIN", GOVERNANCE_DOMAIN_NAME)
    if existing:
        return existing["urn"]
    data = emitter.graphql(
        """
        mutation createDomain($input: CreateDomainInput!) {
          createDomain(input: $input)
        }
        """,
        {"input": {"name": GOVERNANCE_DOMAIN_NAME, "description": GOVERNANCE_DOMAIN_DESCRIPTION}},
    )
    return data["createDomain"]


def emit_data_product(emitter: DataHubEmitter, product: DataProduct) -> str:
    domain_urn = ensure_governance_domain(emitter)
    existing = find_data_product_in_domain(emitter, product.id, domain_urn)
    if existing:
        urn = existing["urn"]
        emitter.graphql(
            """
            mutation updateDataProduct($urn: String!, $input: UpdateDataProductInput!) {
              updateDataProduct(urn: $urn, input: $input) { urn }
            }
            """,
            {"urn": urn, "input": {"name": product.id, "description": product.description}},
        )
    else:
        data = emitter.graphql(
            """
            mutation createDataProduct($input: CreateDataProductInput!) {
              createDataProduct(input: $input) { urn }
            }
            """,
            {
                "input": {
                    "domainUrn": domain_urn,
                    "properties": {"name": product.id, "description": product.description},
                }
            },
        )
        urn = data["createDataProduct"]["urn"]
    emitter.emit(urn, "dataProduct", "globalTags", tag_associations(product.tags))
    return urn


def batch_set_data_product(emitter: DataHubEmitter, product_urn: str, resource_urns: tuple[str, ...]) -> None:
    emitter.graphql(
        """
        mutation batchSetDataProduct($input: BatchSetDataProductInput!) {
          batchSetDataProduct(input: $input)
        }
        """,
        {"input": {"dataProductUrn": product_urn, "resourceUrns": list(resource_urns)}},
    )


def emit_dataset(emitter: DataHubEmitter, dataset: Dataset) -> None:
    emitter.emit(
        dataset.urn,
        "dataset",
        "datasetProperties",
        {
            "name": dataset.name,
            "description": dataset.description,
            "customProperties": dataset.custom_properties,
        },
    )
    emitter.emit(dataset.urn, "dataset", "globalTags", tag_associations(dataset.tags))
    if dataset.upstreams:
        emitter.emit(
            dataset.urn,
            "dataset",
            "upstreamLineage",
            {
                "upstreams": [
                    {
                        "dataset": upstream,
                        "type": "TRANSFORMED",
                    }
                    for upstream in dataset.upstreams
                ],
                "fineGrainedLineages": [],
            },
        )


def emit_flow(emitter: DataHubEmitter, product: DataProduct) -> str:
    urn = flow_urn(product.flow_id)
    emitter.emit(
        urn,
        "dataFlow",
        "dataFlowInfo",
        {
            "name": product.flow_name,
            "description": product.description,
            "project": "recsys-data-platform",
            "externalUrl": "http://airflow-webserver.recsys-dataflow.svc.cluster.local:8080",
            "customProperties": {
                "data_product": product.id,
                "orchestrator": "Airflow k8s_data_platform_dag",
            },
        },
    )
    emitter.emit(urn, "dataFlow", "globalTags", tag_associations(product.tags))
    return urn


def emit_job(emitter: DataHubEmitter, flow: str, job: Job) -> None:
    urn = job_urn(flow, job.id)
    emitter.emit(
        urn,
        "dataJob",
        "dataJobInfo",
        {
            "name": job.name,
            "type": {"string": "COMMAND"},
            "description": job.description,
            "customProperties": job.custom_properties,
        },
    )
    emitter.emit(
        urn,
        "dataJob",
        "dataJobInputOutput",
        {
            "inputDatasets": list(job.inputs),
            "outputDatasets": list(job.outputs),
            "inputDatajobs": [],
        },
    )
    emitter.emit(urn, "dataJob", "globalTags", tag_associations(job.tags))


def dp1() -> DataProduct:
    source_tables = [
        "users",
        "user_preferences",
        "products",
        "product_snapshots",
        "sessions",
        "recommendation_requests",
        "impressions",
        "behavior_events",
        "orders",
        "order_items",
    ]
    source = tuple(
        Dataset(
            urn=dataset_urn("postgres", f"source_postgres.recsys.public.{table}"),
            name=f"source_postgres.public.{table}",
            description=(
                "DP1 source OLTP table captured by Debezium CDC. "
                "Data contract is defined in ingest.postgres_cdc_contracts with primary-key and topic mapping."
            ),
            tags=("DP1", "DataContract", "ValidationPassed"),
            custom_properties={
                "data_product": "DP1",
                "contract": "SourceTableContract primary key plus Debezium topic mapping",
                "validation": "validate_bronze_cdc waits for CDC records before downstream promotion",
            },
        )
        for table in source_tables
    )
    topics = tuple(
        Dataset(
            urn=dataset_urn("kafka", f"recsys-dataflow.cdc.{table}"),
            name=f"cdc.{table}",
            description="DP1 Kafka CDC topic emitted by the Debezium source connector.",
            tags=("DP1", "DataContract", "ValidationPassed"),
            custom_properties={
                "data_product": "DP1",
                "contract": "Debezium envelope keyed by source table primary key",
                "validation": "Kafka Connect connector status RUNNING and bronze CDC record-count smoke check",
            },
            upstreams=(dataset_urn("postgres", f"source_postgres.recsys.public.{table}"),),
        )
        for table in source_tables
    )
    bronze = tuple(
        Dataset(
            urn=dataset_urn("s3", f"s3://recsys-lake/bronze/kafka/cdc.{table}"),
            name=f"recsys-lake.bronze.kafka.cdc.{table}",
            description="DP1 immutable bronze CDC files written by Kafka Connect S3/MinIO sink.",
            tags=("DP1", "DataContract", "ValidationPassed"),
            custom_properties={
                "data_product": "DP1",
                "contract": "Raw CDC payload retained with source topic and offset lineage",
                "validation": "infra/docker/scripts/validate_bronze_cdc.py checks minimum records in bronze",
            },
            upstreams=(dataset_urn("kafka", f"recsys-dataflow.cdc.{table}"),),
        )
        for table in source_tables
    )
    return DataProduct(
        id="DP1",
        flow_id="DP1_CDC_Ingestion",
        flow_name="DP1 CDC Ingestion",
        description="Source Postgres to Kafka CDC to MinIO bronze ingestion for realtime RecSys events and dimensions.",
        tags=("DP1", "DataContract", "ValidationPassed"),
        datasets=source + topics + bronze,
        jobs=(
            Job(
                id="register_debezium_connector",
                name="Register Debezium Connector",
                description="Creates the Postgres CDC connector and links source tables to cdc.* Kafka topics.",
                inputs=tuple(item.urn for item in source),
                outputs=tuple(item.urn for item in topics),
                tags=("DP1", "DataContract", "ValidationPassed"),
                custom_properties={"contract": "ingest.postgres_cdc_contracts.SOURCE_TABLE_CONTRACTS"},
            ),
            Job(
                id="register_kafka_minio_sink",
                name="Register Kafka MinIO Raw Sink",
                description="Persists cdc.* Kafka topics into the bronze MinIO lake.",
                inputs=tuple(item.urn for item in topics),
                outputs=tuple(item.urn for item in bronze),
                tags=("DP1", "DataContract", "ValidationPassed"),
                custom_properties={"validation": "validate_bronze_cdc.py min-record gate"},
            ),
        ),
    )


def dp2() -> DataProduct:
    bronze_behavior = dataset_urn("s3", "s3://recsys-lake/bronze/kafka/cdc.behavior_events")
    staging_tables = (
        Dataset(
            urn=dataset_urn("postgres", "warehouse_postgres.recsys_warehouse.staging.stream_behavior_events"),
            name="staging.stream_behavior_events",
            description="DP2 staging table populated by the Flink realtime stream job.",
            tags=("DP2", "DataContract", "ValidationPassed"),
            custom_properties={
                "contract": "STAGING_STREAM_BEHAVIOR_EVENTS TableSpec",
                "validation": "Great Expectations staging contract: required columns, uniqueness, freshness, skew checks",
            },
            upstreams=(bronze_behavior,),
        ),
        Dataset(
            urn=dataset_urn("postgres", "warehouse_postgres.recsys_warehouse.staging.stream_user_sequence_features"),
            name="staging.stream_user_sequence_features",
            description="DP2 staging sequence feature table from streaming feature engineering.",
            tags=("DP2", "DataContract", "ValidationPassed"),
            custom_properties={
                "contract": "STAGING_STREAM_USER_SEQUENCE_FEATURES TableSpec",
                "validation": "Great Expectations staging contract",
            },
            upstreams=(bronze_behavior,),
        ),
        Dataset(
            urn=dataset_urn("postgres", "warehouse_postgres.recsys_warehouse.staging.stream_user_aggregate_features"),
            name="staging.stream_user_aggregate_features",
            description="DP2 staging aggregate feature table from streaming feature engineering.",
            tags=("DP2", "DataContract", "ValidationPassed"),
            custom_properties={
                "contract": "STAGING_STREAM_USER_AGGREGATE_FEATURES TableSpec",
                "validation": "Great Expectations staging contract",
            },
            upstreams=(bronze_behavior,),
        ),
        Dataset(
            urn=dataset_urn("postgres", "warehouse_postgres.recsys_warehouse.staging.stream_item_features"),
            name="staging.stream_item_features",
            description="DP2 staging item feature table from streaming feature engineering.",
            tags=("DP2", "DataContract", "ValidationPassed"),
            custom_properties={
                "contract": "STAGING_STREAM_ITEM_FEATURES TableSpec",
                "validation": "Great Expectations staging contract",
            },
            upstreams=(bronze_behavior,),
        ),
    )
    production = tuple(
        Dataset(
            urn=dataset_urn("postgres", f"warehouse_postgres.recsys_warehouse.production.{name}"),
            name=f"production.{name}",
            description="DP2 dbt production model with dbt schema.yml tests for not-null, uniqueness, and accepted values.",
            tags=("DP2", "DataContract", "ValidationPassed"),
            custom_properties={
                "contract": "apps/data-platform/dbt/recsys_warehouse/models/production/schema.yml",
                "validation": "dbt build tests plus upstream Great Expectations staging gate",
            },
            upstreams=tuple(table.urn for table in staging_tables),
        )
        for name in ("fact_behavior_events", "fact_impressions", "fact_orders", "dim_products_scd")
    )
    return DataProduct(
        id="DP2",
        flow_id="DP2_Warehouse_Transform",
        flow_name="DP2 Warehouse Transform",
        description="Bronze/staging data quality gate and dbt production warehouse transform.",
        tags=("DP2", "DataContract", "ValidationPassed"),
        datasets=staging_tables + production,
        jobs=(
            Job(
                id="run_flink_processing",
                name="Run Flink Processing",
                description="Consumes cdc.behavior_events and writes realtime staging feature tables.",
                inputs=(bronze_behavior,),
                outputs=tuple(table.urn for table in staging_tables),
                tags=("DP2", "DataContract", "ValidationPassed"),
                custom_properties={"validation": "Long-running Flink consumer plus warehouse staging row checks"},
            ),
            Job(
                id="ge_validate_staging",
                name="GE Validate Staging",
                description="Runs Great Expectations staging contracts before production promotion.",
                inputs=tuple(table.urn for table in staging_tables),
                outputs=(dataset_urn("s3", "s3://recsys-lake/monitoring/great_expectations/staging_validation.json"),),
                tags=("DP2", "DataContract", "ValidationPassed"),
                custom_properties={"contract": "STAGING_TABLE_CONTRACTS"},
            ),
            Job(
                id="dbt_transform_production",
                name="dbt Transform Production",
                description="Builds production fact and dimension models from validated staging inputs.",
                inputs=tuple(table.urn for table in staging_tables),
                outputs=tuple(table.urn for table in production),
                tags=("DP2", "DataContract", "ValidationPassed"),
                custom_properties={"contract": "dbt schema.yml tests"},
            ),
        ),
    )


def dp3() -> DataProduct:
    production_inputs = tuple(
        dataset_urn("postgres", f"warehouse_postgres.recsys_warehouse.production.{name}")
        for name in ("fact_behavior_events", "fact_impressions", "fact_orders", "dim_products_scd")
    )
    offline = tuple(
        Dataset(
            urn=dataset_urn("s3", f"s3://recsys-feature-store/offline/{name}"),
            name=f"feast_offline.{name}",
            description="DP3 Feast offline feature parquet generated from warehouse production models.",
            tags=("DP3", "DataContract", "ValidationPassed"),
            custom_properties={
                "contract": "Feast FileSource plus FeatureView schema",
                "validation": "validate_feature_store.py verifies offline feature columns before materialization",
            },
            upstreams=production_inputs,
        )
        for name in ("user_sequence_features", "user_aggregate_features", "item_features")
    )
    online = tuple(
        Dataset(
            urn=dataset_urn("redis", f"redis://redis.recsys-dataflow.svc.cluster.local:6379/feast/{name}"),
            name=f"feast_online.{name}",
            description="DP3 Feast online feature view materialized incrementally into Redis serving keys.",
            tags=("DP3", "DataContract", "ValidationPassed"),
            custom_properties={
                "contract": "Feast FeatureView online=True schema",
                "validation": "materialize-incremental run plus registry backup checkpoint",
            },
            upstreams=(dataset_urn("s3", f"s3://recsys-feature-store/offline/{name}"),),
        )
        for name in ("user_sequence_features", "user_aggregate_features", "item_features")
    )
    return DataProduct(
        id="DP3",
        flow_id="DP3_Feature_Store_Materialization",
        flow_name="DP3 Feature Store Materialization",
        description="Production warehouse to Feast offline store to Redis online feature store sync.",
        tags=("DP3", "DataContract", "ValidationPassed"),
        datasets=offline + online,
        jobs=(
            Job(
                id="write_offline_feature_store",
                name="Write Offline Feature Store",
                description="Exports Feast offline feature parquet from dbt production warehouse tables.",
                inputs=production_inputs,
                outputs=tuple(item.urn for item in offline),
                tags=("DP3", "DataContract", "ValidationPassed"),
                custom_properties={"contract": "feature_repo/data_sources.py and feature_views.py"},
            ),
            Job(
                id="validate_offline_feature_store",
                name="Validate Offline Feature Store",
                description="Checks Feast offline datasets satisfy expected feature columns.",
                inputs=tuple(item.urn for item in offline),
                outputs=tuple(item.urn for item in offline),
                tags=("DP3", "DataContract", "ValidationPassed"),
                custom_properties={"validation": "apps/data-platform/feature-store/src/validate_feature_store.py"},
            ),
            Job(
                id="sync_offline_to_online_store",
                name="Sync Offline To Online Store",
                description="Runs Feast apply and materialize-incremental to sync offline features into Redis.",
                inputs=tuple(item.urn for item in offline),
                outputs=tuple(item.urn for item in online),
                tags=("DP3", "DataContract", "ValidationPassed"),
                custom_properties={"materialization": "feast materialize-incremental"},
            ),
        ),
    )


def emit_products(emitter: DataHubEmitter, products: tuple[DataProduct, ...]) -> dict[str, str]:
    for tag, description, color in (
        ("DP1", "Data product DP1: CDC ingestion from source Postgres to Kafka and bronze lake.", "#2E7D32"),
        ("DP2", "Data product DP2: staging validation and production warehouse transform.", "#1565C0"),
        ("DP3", "Data product DP3: Feast offline to Redis online feature materialization.", "#6A1B9A"),
        ("DataContract", "Entity has an explicit schema or pipeline contract in the RecSys data platform repo.", "#455A64"),
        ("ValidationPassed", "Entity is protected by validation gates such as CDC smoke checks, Great Expectations, dbt tests, or Feast checks.", "#00897B"),
    ):
        emit_tag(emitter, tag, description, color)

    product_urns = {}
    for product in products:
        product_urn = emit_data_product(emitter, product)
        product_urns[product.id] = product_urn
        for dataset in product.datasets:
            emit_dataset(emitter, dataset)
        flow = emit_flow(emitter, product)
        for job in product.jobs:
            emit_job(emitter, flow, job)
        resource_urns = tuple(item.urn for item in product.datasets) + (flow,) + tuple(job_urn(flow, job.id) for job in product.jobs)
        batch_set_data_product(emitter, product_urn, resource_urns)
    return product_urns


def datahub_metric_samples(summary: dict[str, Any]) -> list[MetricSample]:
    ingested = 1.0 if summary.get("ingested") else 0.0
    samples = [
        MetricSample("recsys_datahub_ingest_success", ingested),
        MetricSample("recsys_datahub_ingest_timestamp_seconds", float(int(time.time()))),
        MetricSample("recsys_datahub_ingest_dataset_count", float(summary.get("datasets", 0))),
        MetricSample("recsys_datahub_ingest_job_count", float(summary.get("jobs", 0))),
        MetricSample("recsys_datahub_ingest_data_product_count", float(len(summary.get("data_products", [])))),
    ]
    for product in summary.get("data_products", []):
        samples.append(MetricSample("recsys_datahub_ingest_data_product_present", 1.0, {"data_product": str(product)}))
    return samples


def push_datahub_ingest_metrics(summary: dict[str, Any], pushgateway_url: str | None) -> None:
    push_metrics(
        datahub_metric_samples(summary),
        "recsys_datahub_governance",
        gateway_url=pushgateway_url,
        grouping_key={"gms_url": summary.get("gms_url", "unknown").replace("/", "_")},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest RecSys DP1/DP2/DP3 governance metadata into DataHub.")
    parser.add_argument("--gms-url", default="http://localhost:8088", help="DataHub GMS base URL.")
    parser.add_argument("--pushgateway-url", default=os.getenv("PUSHGATEWAY_URL", ""), help="Optional Pushgateway URL for ingest metrics.")
    parser.add_argument("--strict", action="store_true", default=os.getenv("DATAHUB_INGEST_STRICT", "").lower() in {"1", "true", "yes"})
    args = parser.parse_args()
    products = (dp1(), dp2(), dp3())
    emitter = DataHubEmitter(args.gms_url)
    try:
        product_urns = emit_products(emitter, products)
    except Exception as exc:
        summary = {
            "data_products": [product.id for product in products],
            "gms_url": args.gms_url,
            "ingested": False,
            "error": str(exc),
        }
        push_datahub_ingest_metrics(summary, args.pushgateway_url or None)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if args.strict else 0
    summary = {
        "data_products": [product.id for product in products],
        "data_product_entities": product_urns,
        "datasets": sum(len(product.datasets) for product in products),
        "jobs": sum(len(product.jobs) for product in products),
        "gms_url": args.gms_url,
        "ingested": True,
    }
    push_datahub_ingest_metrics(summary, args.pushgateway_url or None)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
