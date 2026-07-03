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
            tags=("DP1", "DataContract", "NativePipeline"),
            custom_properties={
                "data_product": "DP1",
                "contract": "SourceTableContract primary key plus Debezium topic mapping",
            },
        )
        for table in source_tables
    )
    topics = tuple(
        Dataset(
            urn=dataset_urn("kafka", f"recsys-dataflow.cdc.{table}"),
            name=f"cdc.{table}",
            description="DP1 Kafka CDC topic emitted by the Debezium source connector.",
            tags=("DP1", "DataContract", "NativePipeline"),
            custom_properties={
                "data_product": "DP1",
                "contract": "Debezium envelope keyed by source table primary key",
            },
            upstreams=(dataset_urn("postgres", f"source_postgres.recsys.public.{table}"),),
        )
        for table in source_tables
    )
    return DataProduct(
        id="DP1",
        flow_id="DP1_CDC_Ingestion",
        flow_name="DP1 CDC Ingestion",
        description="Source Postgres WAL captured by Debezium and published to cdc.* Kafka topics.",
        tags=("DP1", "DataContract", "NativePipeline"),
        datasets=source + topics,
        jobs=(
            Job(
                id="register_debezium_connector",
                name="Register Debezium Connector",
                description="Creates the Postgres CDC connector and links source tables to cdc.* Kafka topics.",
                inputs=tuple(item.urn for item in source),
                outputs=tuple(item.urn for item in topics),
                tags=("DP1", "DataContract", "NativePipeline"),
                custom_properties={"contract": "ingest.postgres_cdc_contracts.SOURCE_TABLE_CONTRACTS"},
            ),
        ),
    )


def dp2() -> DataProduct:
    behavior_topic = dataset_urn("kafka", "recsys-dataflow.cdc.behavior_events")
    stream_tables = (
        Dataset(
            urn=dataset_urn("postgres", "feature-postgres.feature_store.user_sequence_features"),
            name="postgres.feature_store.user_sequence_features",
            description="DP2 PostgreSQL Feast user sequence feature table written continuously by native PyFlink.",
            tags=("DP2", "DataContract", "NativePipeline"),
            custom_properties={"contract": "Feast PostgreSQLSource user_sequence_features table"},
            upstreams=(behavior_topic,),
        ),
        Dataset(
            urn=dataset_urn("postgres", "feature-postgres.feature_store.user_aggregate_features"),
            name="postgres.feature_store.user_aggregate_features",
            description="DP2 PostgreSQL Feast user aggregate feature table written continuously by native PyFlink.",
            tags=("DP2", "DataContract", "NativePipeline"),
            custom_properties={"contract": "Feast PostgreSQLSource user_aggregate_features table"},
            upstreams=(behavior_topic,),
        ),
        Dataset(
            urn=dataset_urn("postgres", "feature-postgres.feature_store.item_features"),
            name="postgres.feature_store.item_features",
            description="DP2 PostgreSQL Feast item feature table written continuously by native PyFlink.",
            tags=("DP2", "DataContract", "NativePipeline"),
            custom_properties={"contract": "Feast PostgreSQLSource item_features table"},
            upstreams=(behavior_topic,),
        ),
    )
    return DataProduct(
        id="DP2",
        flow_id="DP2_Stream_PostgreSQL_Feast_Features",
        flow_name="DP2 Stream PostgreSQL Feast Features",
        description="Native PyFlink stream processing from Kafka into the PostgreSQL Feast offline feature store.",
        tags=("DP2", "DataContract", "NativePipeline"),
        datasets=stream_tables,
        jobs=(
            Job(
                id="run_flink_stream_to_feature_stores",
                name="Run Flink Stream To Feature Stores",
                description="Consumes cdc.behavior_events and writes PostgreSQL Feast offline tables plus Redis online keys.",
                inputs=(behavior_topic,),
                outputs=tuple(table.urn for table in stream_tables),
                tags=("DP2", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PyFlink DataStream plus psycopg PostgreSQL inserts"},
            ),
        ),
    )


def dp3() -> DataProduct:
    batch_lakehouse = tuple(
        Dataset(
            urn=dataset_urn("s3", f"s3://recsys-lakehouse/warehouse/lakehouse/{name}"),
            name=f"parquet.recsys_lakehouse.lakehouse.{name}",
            description="Historical generator batch table ingested into the Parquet data lakehouse.",
            tags=("DP3", "DataContract", "NativePipeline"),
            custom_properties={"contract": "Parquet lakehouse raw generator table"},
        )
        for name in ("users", "products", "behavior_events", "impressions", "orders")
    )
    offline = tuple(
        Dataset(
            urn=dataset_urn("s3", f"s3://recsys-offline-feature-store/warehouse/feature_store/{name}"),
            name=f"iceberg.recsys_features.feature_store.{name}",
            description="DP3 Iceberg offline feature table generated by native PySpark batch.",
            tags=("DP3", "DataContract", "NativePipeline"),
            custom_properties={"contract": "Iceberg offline feature table"},
            upstreams=tuple(table.urn for table in batch_lakehouse),
        )
        for name in ("user_sequence_features", "user_aggregate_features", "item_features")
    )
    online = tuple(
        Dataset(
            urn=dataset_urn("redis", f"redis://redis.recsys-dataflow.svc.cluster.local:6379/{name}"),
            name=f"redis_online.{name}",
            description="DP3 Redis online feature keys written directly by native PyFlink.",
            tags=("DP3", "DataContract", "NativePipeline"),
            custom_properties={"contract": "feature_store.online_writer.RedisOnlineWriter"},
            upstreams=(dataset_urn("kafka", "recsys-dataflow.cdc.behavior_events"),),
        )
        for name in ("user_sequence_features", "user_aggregate_features", "item_features")
    )
    return DataProduct(
        id="DP3",
        flow_id="DP3_Native_Feature_Stores",
        flow_name="DP3 Native Feature Stores",
        description="Python batch ingestion writes Parquet lakehouse raw tables; PySpark batch writes Iceberg offline feature store; PyFlink stream writes Redis online feature store.",
        tags=("DP3", "DataContract", "NativePipeline"),
        datasets=batch_lakehouse + offline + online,
        jobs=(
            Job(
                id="ingest_historical_batch_to_lakehouse",
                name="Ingest Historical Batch To Lakehouse",
                description="Loads generated historical parquet into Parquet raw lakehouse tables.",
                inputs=(),
                outputs=tuple(item.urn for item in batch_lakehouse),
                tags=("DP3", "DataContract", "NativePipeline"),
                custom_properties={"engine": "Python pyarrow parquet writer"},
            ),
            Job(
                id="run_spark_batch_to_offline_store",
                name="Run Spark Batch To Offline Store",
                description="Reads Parquet lakehouse tables, builds batch feature views with PySpark, and writes Iceberg offline feature tables.",
                inputs=tuple(item.urn for item in batch_lakehouse),
                outputs=tuple(item.urn for item in offline),
                tags=("DP3", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PySpark plus Iceberg Spark writer"},
            ),
            Job(
                id="run_flink_stream_to_online_store",
                name="Run Flink Stream To Online Store",
                description="Updates Redis online feature keys directly from the native PyFlink stream.",
                inputs=(dataset_urn("kafka", "recsys-dataflow.cdc.behavior_events"),),
                outputs=tuple(item.urn for item in online),
                tags=("DP3", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PyFlink DataStream plus Redis sink"},
            ),
        ),
    )


def emit_products(emitter: DataHubEmitter, products: tuple[DataProduct, ...]) -> dict[str, str]:
    for tag, description, color in (
        ("DP1", "Data product DP1: CDC ingestion from source Postgres to Kafka.", "#2E7D32"),
        ("DP2", "Data product DP2: native Flink stream to Iceberg lakehouse.", "#1565C0"),
        ("DP3", "Data product DP3: Iceberg offline and Redis online feature stores.", "#6A1B9A"),
        ("DataContract", "Entity has an explicit schema or pipeline contract in the RecSys data platform repo.", "#455A64"),
        ("NativePipeline", "Entity is produced by Spark, Flink, Debezium, Iceberg, or Redis native runtime.", "#00897B"),
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
