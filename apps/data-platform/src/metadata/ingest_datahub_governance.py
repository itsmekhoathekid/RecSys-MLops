from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from datahub.emitter.aspect import ASPECT_MAP
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DataHubRestEmitter
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

from lakehouse.iceberg import RAW_GENERATOR_TABLES, SILVER_LAKEHOUSE_TABLES
from metadata.governance_catalog import (
    BRONZE_URNS,
    ENV,
    ICEBERG_FEATURE_URNS,
    KAFKA_TOPIC_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
    SILVER_URNS,
    SOURCE_POSTGRES_URNS,
    assertion_urn,
    data_contract_id,
    dataset_urn_parts,
    flow_urn,
    job_urn,
    pipeline_flow_id,
)
from metadata.governance_schemas import (
    FEATURE_PRIMARY_KEYS,
    RAW_PRIMARY_KEYS,
    SILVER_PRIMARY_KEYS,
    SchemaColumn,
    bronze_schema,
    cdc_topic_schema,
    feature_schema,
    raw_schema,
    silver_schema,
)
from monitoring.pushgateway import MetricSample, push_metrics


ACTOR = "urn:li:corpuser:datahub"
GOVERNANCE_DOMAIN_NAME = "RecSys Data Platform"
GOVERNANCE_DOMAIN_DESCRIPTION = (
    "Governed batch, CDC, and streaming pipelines for the RecSys data platform."
)


@dataclass(frozen=True)
class Dataset:
    urn: str
    name: str
    description: str
    tags: tuple[str, ...]
    custom_properties: dict[str, str]
    schema: tuple[SchemaColumn, ...]
    primary_keys: tuple[str, ...] = ()
    validation_pipeline: str | None = None
    required_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    description: str
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


class GovernanceEmitter(DataHubRestEmitter):
    """Official DataHub MCP emitter paired with its native graph client."""

    def __init__(self, gms_url: str) -> None:
        self.gms_url = gms_url.rstrip("/")
        token = (
            os.getenv("DATAHUB_TOKEN") or os.getenv("DATAHUB_GMS_TOKEN") or ""
        ).strip()
        super().__init__(
            gms_server=self.gms_url,
            token=token or None,
            timeout_sec=180,
            retry_status_codes=[408, 425, 429, *range(500, 600)],
            retry_methods=["POST"],
            retry_max_times=5,
            openapi_ingestion=False,
            datahub_component="recsys-governance-ingestion",
        )
        self.graph = DataHubGraph(
            DatahubClientConfig(
                server=self.gms_url,
                token=token or None,
                timeout_sec=180,
                retry_status_codes=[408, 425, 429, *range(500, 600)],
                retry_max_times=5,
                datahub_component="recsys-governance-ingestion",
            )
        )

    def close(self) -> None:
        try:
            self.graph.close()
        finally:
            super().close()


def emit_aspect(
    emitter: DataHubRestEmitter,
    entity_urn: str,
    aspect_name: str,
    aspect: dict[str, Any],
) -> None:
    try:
        aspect_type = ASPECT_MAP[aspect_name]
    except KeyError as exc:
        raise ValueError(f"Unknown DataHub aspect: {aspect_name}") from exc
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=entity_urn,
            aspect=aspect_type.from_obj(aspect),
        )
    )


def audit_stamp() -> dict[str, Any]:
    return {"time": int(time.time() * 1000), "actor": ACTOR}


def tag_associations(tags: tuple[str, ...]) -> dict[str, Any]:
    return {"tags": [{"tag": f"urn:li:tag:{tag}"} for tag in tags]}


def emit_tag(
    emitter: GovernanceEmitter, tag: str, description: str, color_hex: str
) -> None:
    emit_aspect(
        emitter,
        f"urn:li:tag:{tag}",
        "tagProperties",
        {
            "name": tag,
            "description": description,
            "colorHex": color_hex,
        },
    )


def find_entity_by_exact_name(
    emitter: GovernanceEmitter, entity_type: str, name: str
) -> dict[str, Any] | None:
    data = emitter.graph.execute_graphql(
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


def find_data_product_in_domain(
    emitter: GovernanceEmitter, product_id: str, domain_urn: str
) -> dict[str, Any] | None:
    data = emitter.graph.execute_graphql(
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
        {
            "input": {
                "types": ["DATA_PRODUCT"],
                "query": product_id,
                "start": 0,
                "count": 25,
            }
        },
    )
    for result in data["searchAcrossEntities"]["searchResults"]:
        entity = result["entity"]
        if (
            entity.get("properties", {}).get("name") == product_id
            and (entity.get("domain") or {}).get("domain", {}).get("urn") == domain_urn
        ):
            return entity
    return None


def ensure_governance_domain(emitter: GovernanceEmitter) -> str:
    existing = find_entity_by_exact_name(emitter, "DOMAIN", GOVERNANCE_DOMAIN_NAME)
    if existing:
        return existing["urn"]
    data = emitter.graph.execute_graphql(
        """
        mutation createDomain($input: CreateDomainInput!) {
          createDomain(input: $input)
        }
        """,
        {
            "input": {
                "name": GOVERNANCE_DOMAIN_NAME,
                "description": GOVERNANCE_DOMAIN_DESCRIPTION,
            }
        },
    )
    return data["createDomain"]


def emit_data_product(emitter: GovernanceEmitter, product: DataProduct) -> str:
    domain_urn = ensure_governance_domain(emitter)
    existing = find_data_product_in_domain(emitter, product.id, domain_urn)
    if existing:
        urn = existing["urn"]
        emitter.graph.execute_graphql(
            """
            mutation updateDataProduct($urn: String!, $input: UpdateDataProductInput!) {
              updateDataProduct(urn: $urn, input: $input) { urn }
            }
            """,
            {
                "urn": urn,
                "input": {"name": product.id, "description": product.description},
            },
        )
    else:
        data = emitter.graph.execute_graphql(
            """
            mutation createDataProduct($input: CreateDataProductInput!) {
              createDataProduct(input: $input) { urn }
            }
            """,
            {
                "input": {
                    "domainUrn": domain_urn,
                    "properties": {
                        "name": product.id,
                        "description": product.description,
                    },
                }
            },
        )
        urn = data["createDataProduct"]["urn"]
    emit_aspect(emitter, urn, "globalTags", tag_associations(product.tags))
    return urn


def batch_set_data_product(
    emitter: GovernanceEmitter,
    product: DataProduct,
    product_urn: str,
    resource_urns: tuple[str, ...],
) -> None:
    emitter.graph.execute_graphql(
        """
        mutation batchSetDataProduct($input: BatchSetDataProductInput!) {
          batchSetDataProduct(input: $input)
        }
        """,
        {"input": {"dataProductUrn": product_urn, "resourceUrns": list(resource_urns)}},
    )
    stamp = audit_stamp()
    emit_aspect(
        emitter,
        product_urn,
        "dataProductProperties",
        {
            "name": product.id,
            "description": product.description,
            "assets": [
                {
                    "destinationUrn": resource_urn,
                    "created": stamp,
                    "lastModified": stamp,
                }
                for resource_urn in resource_urns
            ],
        },
    )


def emit_dataset(emitter: GovernanceEmitter, dataset: Dataset) -> None:
    emit_aspect(
        emitter,
        dataset.urn,
        "datasetProperties",
        {
            "name": dataset.name,
            "description": dataset.description,
            "customProperties": dataset.custom_properties,
        },
    )
    emit_aspect(emitter, dataset.urn, "globalTags", tag_associations(dataset.tags))
    emit_aspect(emitter, dataset.urn, "schemaMetadata", schema_metadata(dataset))
    emit_aspect(
        emitter,
        dataset.urn,
        "upstreamLineage",
        {
            # Direct dataset lineage used to be declared in this catalog. Emptying
            # the aspect removes those stale edges; runtime DataJob events are now
            # the only lineage source.
            "upstreams": [],
            "fineGrainedLineages": [],
        },
    )
    if dataset.validation_pipeline:
        emit_dataset_contract(emitter, dataset)


def _platform_urn(dataset_urn: str) -> str:
    match = re.search(r"urn:li:dataPlatform:([^,]+)", dataset_urn)
    if not match:
        raise ValueError(f"Dataset URN does not contain a data platform: {dataset_urn}")
    return f"urn:li:dataPlatform:{match.group(1)}"


def _datahub_type(native_type: str) -> str:
    normalized = native_type.upper()
    if "ARRAY" in normalized or normalized.endswith("[]"):
        return "ArrayType"
    if any(token in normalized for token in ("STRUCT", "RECORD", "MAP", "JSON")):
        return "RecordType"
    if "DATE" in normalized and "TIME" not in normalized:
        return "DateType"
    if any(token in normalized for token in ("TIME", "TIMESTAMP")):
        return "TimeType"
    if "BOOL" in normalized:
        return "BooleanType"
    if any(
        token in normalized
        for token in ("INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL")
    ):
        return "NumberType"
    if any(token in normalized for token in ("BINARY", "BYTES", "BLOB")):
        return "BytesType"
    return "StringType"


def schema_metadata(dataset: Dataset) -> dict[str, Any]:
    stamp = audit_stamp()
    fields = [
        {
            "fieldPath": column.name,
            "nullable": column.nullable,
            "description": column.description
            or f"{column.name} field in {dataset.name}.",
            "type": {
                "type": {f"com.linkedin.schema.{_datahub_type(column.native_type)}": {}}
            },
            "nativeDataType": column.native_type,
            "recursive": False,
            "isPartOfKey": column.name in dataset.primary_keys,
            "lastModified": stamp,
        }
        for column in dataset.schema
    ]
    raw_schema = {
        "name": dataset.name,
        "type": "record",
        "fields": [
            {
                "name": column.name,
                "type": column.native_type,
                "nullable": column.nullable,
            }
            for column in dataset.schema
        ],
    }
    return {
        "schemaName": dataset.name,
        "platform": _platform_urn(dataset.urn),
        "version": 0,
        "hash": "",
        "platformSchema": {
            "com.linkedin.schema.OtherSchema": {
                "rawSchema": json.dumps(raw_schema, sort_keys=True)
            }
        },
        "fields": fields,
        "primaryKeys": list(dataset.primary_keys),
        "created": stamp,
        "lastModified": stamp,
    }


def schema_assertion_info(dataset: Dataset) -> dict[str, Any]:
    return {
        "type": "DATA_SCHEMA",
        "schemaAssertion": {
            "entity": dataset.urn,
            "schema": schema_metadata(dataset),
            "compatibility": "EXACT_MATCH",
        },
        "source": {"type": "EXTERNAL"},
        "lastUpdated": audit_stamp(),
        "description": f"Schema validation for {dataset.name}",
        "customProperties": {
            "pipeline": dataset.validation_pipeline or "unknown",
            "required_columns": json.dumps(dataset.required_columns),
        },
    }


def _emit_assertion(
    emitter: GovernanceEmitter,
    dataset: Dataset,
    *,
    assertion_type: str,
) -> str:
    urn = assertion_urn(dataset.urn, assertion_type.lower())
    if assertion_type == "SCHEMA":
        emit_aspect(emitter, urn, "assertionInfo", schema_assertion_info(dataset))
        assertion = urn
    else:
        assertion = emitter.graph.upsert_custom_assertion(
            urn=urn,
            entity_urn=dataset.urn,
            type=assertion_type,
            description=(
                f"{assertion_type.replace('_', ' ').title()} validation for "
                f"{dataset.name}"
            ),
            platform_name="recsys-native-validation",
            logic=json.dumps(
                {
                    "pipeline": dataset.validation_pipeline,
                    "required_columns": list(dataset.required_columns),
                },
                sort_keys=True,
            ),
        )["urn"]
    return assertion


def emit_dataset_contract(emitter: GovernanceEmitter, dataset: Dataset) -> None:
    schema_assertion = _emit_assertion(
        emitter,
        dataset,
        assertion_type="SCHEMA",
    )
    quality_assertion = _emit_assertion(
        emitter,
        dataset,
        assertion_type="DATA_QUALITY",
    )
    emitter.graph.execute_graphql(
        """
        mutation upsertDataContract($input: UpsertDataContractInput!) {
          upsertDataContract(input: $input) {
            urn
          }
        }
        """,
        {
            "input": {
                "entityUrn": dataset.urn,
                "schema": [{"assertionUrn": schema_assertion}],
                "dataQuality": [{"assertionUrn": quality_assertion}],
                "state": "ACTIVE",
                "id": data_contract_id(dataset.urn),
            }
        },
    )


def emit_flow(emitter: GovernanceEmitter, product: DataProduct) -> str:
    urn = flow_urn(product.flow_id)
    emit_aspect(
        emitter,
        urn,
        "dataFlowInfo",
        {
            "name": product.flow_name,
            "description": product.description,
            "project": "recsys-data-platform",
            "externalUrl": "http://airflow-webserver.recsys-dataflow.svc.cluster.local:8080",
            "customProperties": {
                "data_product": product.id,
                "orchestrator": "Kubernetes CDC and Flink runtime",
            },
        },
    )
    emit_aspect(emitter, urn, "globalTags", tag_associations(product.tags))
    return urn


def emit_job(emitter: GovernanceEmitter, flow: str, job: Job) -> None:
    urn = job_urn(flow, job.id)
    emit_aspect(
        emitter,
        urn,
        "dataJobInfo",
        {
            "name": job.name,
            "type": {"string": "COMMAND"},
            "description": job.description,
            "customProperties": job.custom_properties,
        },
    )
    emit_aspect(emitter, urn, "globalTags", tag_associations(job.tags))


def _dataset(
    urn: str,
    name: str,
    description: str,
    product: str,
    contract: str,
    *,
    schema: tuple[SchemaColumn, ...],
    primary_keys: tuple[str, ...] = (),
    validation_pipeline: str | None = None,
    required_columns: tuple[str, ...] = (),
) -> Dataset:
    return Dataset(
        urn=urn,
        name=name,
        description=description,
        tags=(product, "DataContract", "NativePipeline"),
        custom_properties={"data_product": product, "contract": contract},
        schema=schema,
        primary_keys=primary_keys,
        validation_pipeline=validation_pipeline,
        required_columns=required_columns,
    )


def dp1() -> DataProduct:
    bronze = tuple(
        _dataset(
            BRONZE_URNS[table],
            f"recsys.lakehouse.bronze_{table}",
            "DP1 Bronze Iceberg lakehouse table with source-run and ingestion metadata.",
            "DP1",
            "Non-empty Bronze table with source key, source_run_id, and lakehouse_ingestion_ts",
            schema=bronze_schema(table),
            primary_keys=("source_run_id",) + RAW_PRIMARY_KEYS[table],
            validation_pipeline="DP1",
            required_columns=("source_run_id", "lakehouse_ingestion_ts"),
        )
        for table in RAW_GENERATOR_TABLES
    )
    return DataProduct(
        id="DP1",
        flow_id=pipeline_flow_id("DP1"),
        flow_name="DP1 Data Generator Batch Ingestion To Bronze Lakehouse",
        description="Direct batch ingestion from Data Generator output into the Bronze Iceberg lakehouse.",
        tags=("DP1", "DataContract", "NativePipeline"),
        datasets=bronze,
        jobs=(
            Job(
                id="ingest_stage",
                name="Ingest Stage - Data Generator Batch Ingestion",
                description="Runs the historical Data Generator in the Spark pod and commits its ephemeral output as Bronze Iceberg tables.",
                tags=("DP1", "DataContract", "NativePipeline"),
                custom_properties={"engine": "Data Generator plus PySpark and Iceberg"},
            ),
            Job(
                id="optimize_stage",
                name="Optimize Stage - Bronze Iceberg",
                description="Applies compaction, write sizing, compression, and manifest maintenance to all DP1 Bronze Iceberg tables.",
                tags=("DP1", "DataContract", "NativePipeline", "LakehouseOptimization"),
                custom_properties={"engine": "Apache Iceberg Spark procedures"},
            ),
            Job(
                id="validate_stage",
                name="Validate Stage",
                description="Validates Bronze table existence, row counts, source keys, and ingestion metadata.",
                tags=("DP1", "DataContract", "NativePipeline"),
                custom_properties={"validation": "datahub-custom-assertion-writeback"},
            ),
        ),
    )


def dp2() -> DataProduct:
    silver = tuple(
        _dataset(
            SILVER_URNS[table],
            f"iceberg.recsys.lakehouse.silver_{table}",
            "DP2 curated Silver Iceberg table produced from Bronze Iceberg inputs.",
            "DP2",
            "Readable Silver Iceberg table; clean_behavior_events must be unique by event_id",
            schema=silver_schema(table),
            primary_keys=SILVER_PRIMARY_KEYS[table],
            validation_pipeline="DP2",
            required_columns=("event_id", "event_timestamp", "ingestion_ts")
            if table == "clean_behavior_events"
            else (),
        )
        for table in SILVER_LAKEHOUSE_TABLES
    )
    return DataProduct(
        id="DP2",
        flow_id=pipeline_flow_id("DP2"),
        flow_name="DP2 Bronze To Silver And Gold",
        description="PySpark curation from Bronze Iceberg tables into deduplicated and normalized Silver Iceberg tables.",
        tags=("DP2", "DataContract", "NativePipeline"),
        datasets=silver,
        jobs=(
            Job(
                id="ingest_stage",
                name="Ingest Stage",
                description="Reads Bronze Iceberg, normalizes schemas, deduplicates events, and writes Silver Iceberg tables.",
                tags=("DP2", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PySpark plus Iceberg"},
            ),
            Job(
                id="optimize_stage",
                name="Optimize Stage - Silver Iceberg",
                description="Applies compaction, write sizing, compression, and manifest maintenance to all DP2 Silver Iceberg tables.",
                tags=("DP2", "DataContract", "NativePipeline", "LakehouseOptimization"),
                custom_properties={"engine": "Apache Iceberg Spark procedures"},
            ),
            Job(
                id="validate_stage",
                name="Validate Stage",
                description="Validates Silver outputs and confirms clean behavior events contain no duplicate event_id values.",
                tags=("DP2", "DataContract", "NativePipeline"),
                custom_properties={"validation": "datahub-custom-assertion-writeback"},
            ),
        ),
    )


def dp3() -> DataProduct:
    required = {
        "user_sequence_features": ("user_id", "feature_timestamp"),
        "user_aggregate_features": ("user_id", "feature_timestamp"),
        "item_features": ("product_id", "feature_timestamp"),
        "ml_ranking_labels": ("impression_id", "prediction_timestamp"),
        "ml_bst_training": ("impression_id", "prediction_timestamp"),
    }
    iceberg = tuple(
        _dataset(
            ICEBERG_FEATURE_URNS[table],
            f"iceberg.recsys_features.feature_store.{table}",
            "DP3 batch feature output stored as an Iceberg table.",
            "DP3",
            "Non-empty Iceberg feature output with non-null entity key and feature timestamp",
            schema=feature_schema(table),
            primary_keys=FEATURE_PRIMARY_KEYS[table],
            validation_pipeline="DP3",
            required_columns=required[table],
        )
        for table in ICEBERG_FEATURE_URNS
    )
    postgres = tuple(
        _dataset(
            POSTGRES_FEATURE_URNS[table],
            f"postgres.feature_store.{table}",
            "DP3 PostgreSQL Feast offline feature table exported from the matching Iceberg batch output.",
            "DP3",
            "Feast PostgreSQL offline table with required schema, non-empty rows, and non-null key/timestamp",
            schema=feature_schema(table),
            primary_keys=FEATURE_PRIMARY_KEYS[table],
            validation_pipeline="DP3",
            required_columns=required[table],
        )
        for table in POSTGRES_FEATURE_URNS
    )
    return DataProduct(
        id="DP3",
        flow_id=pipeline_flow_id("DP3"),
        flow_name="DP3 Silver To Feast Offline Features",
        description="PySpark feature engineering from DP2 Silver Iceberg tables into Iceberg features and PostgreSQL Feast offline tables.",
        tags=("DP3", "DataContract", "NativePipeline"),
        datasets=iceberg + postgres,
        jobs=(
            Job(
                id="ingest_stage",
                name="Ingest Stage",
                description="Reads DP2 Silver tables, computes offline features, writes Iceberg outputs, and exports PostgreSQL Feast tables.",
                tags=("DP3", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PySpark, Iceberg, and PostgreSQL"},
            ),
            Job(
                id="validate_stage",
                name="Validate Stage",
                description="Validates both Iceberg feature outputs and PostgreSQL Feast offline-store tables.",
                tags=("DP3", "DataContract", "NativePipeline"),
                custom_properties={"validation": "datahub-custom-assertion-writeback"},
            ),
        ),
    )


def cdc_ingestion() -> DataProduct:
    source = tuple(
        _dataset(
            SOURCE_POSTGRES_URNS[table],
            f"source_postgres.public.{table}",
            "Source PostgreSQL table captured from WAL by Debezium.",
            "CDC_INGESTION",
            "SourceTableContract primary key and Debezium topic mapping",
            schema=raw_schema(table),
            primary_keys=RAW_PRIMARY_KEYS[table],
            validation_pipeline="CDC_INGESTION",
        )
        for table in RAW_GENERATOR_TABLES
    )
    topics = tuple(
        _dataset(
            KAFKA_TOPIC_URNS[table],
            f"cdc.{table}",
            "Kafka CDC topic emitted by the Debezium PostgreSQL connector.",
            "CDC_INGESTION",
            "Debezium change-event envelope keyed by the source primary key",
            schema=cdc_topic_schema(table),
            validation_pipeline="CDC_INGESTION",
        )
        for table in RAW_GENERATOR_TABLES
    )
    return DataProduct(
        id="CDC_INGESTION",
        flow_id=pipeline_flow_id("CDC_INGESTION"),
        flow_name="CDC PostgreSQL To Kafka",
        description="PostgreSQL WAL captured by Debezium and published to cdc.* Kafka topics.",
        tags=("CDC_INGESTION", "DataContract", "NativePipeline"),
        datasets=source + topics,
        jobs=(
            Job(
                id="register_debezium_connector",
                name="Register Debezium Connector",
                description="Registers the Debezium connector linking source PostgreSQL tables to Kafka topics.",
                tags=("CDC_INGESTION", "DataContract", "NativePipeline"),
                custom_properties={
                    "contract": "ingest.postgres_cdc_contracts.SOURCE_TABLE_CONTRACTS"
                },
            ),
        ),
    )


def streaming_features() -> DataProduct:
    redis = tuple(
        _dataset(
            REDIS_FEATURE_URNS[table],
            f"redis_online.{table}",
            "Redis online feature keys updated continuously by the PyFlink online-store job.",
            "STREAMING_FEATURES",
            "Redis online feature entity key and TTL contract",
            schema=feature_schema(table),
            primary_keys=FEATURE_PRIMARY_KEYS[table][:1],
            validation_pipeline="STREAMING_FEATURES",
        )
        for table in REDIS_FEATURE_URNS
    )
    return DataProduct(
        id="STREAMING_FEATURES",
        flow_id=pipeline_flow_id("STREAMING_FEATURES"),
        flow_name="Flink Streaming Features",
        description="Two continuously running Flink jobs consume cdc.behavior_events and update the configured offline store plus Redis online features.",
        tags=("STREAMING_FEATURES", "DataContract", "NativePipeline"),
        datasets=redis,
        jobs=(
            Job(
                id="run_flink_stream_to_offline_store",
                name="Run Flink Stream To Offline Store",
                description="Consumes behavior CDC events and updates the runtime-configured Iceberg or PostgreSQL offline feature tables.",
                tags=("STREAMING_FEATURES", "DataContract", "NativePipeline"),
                custom_properties={
                    "engine": "PyFlink DataStream plus runtime-configured offline sink"
                },
            ),
            Job(
                id="run_flink_stream_to_online_store",
                name="Run Flink Stream To Online Store",
                description="Consumes behavior CDC events and updates Redis online feature keys.",
                tags=("STREAMING_FEATURES", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PyFlink DataStream plus Redis sink"},
            ),
        ),
    )


def verify_governance_coverage(products: tuple[DataProduct, ...]) -> dict[str, Any]:
    known_datasets = {
        dataset.urn for product in products for dataset in product.datasets
    }
    errors: list[str] = []
    if len(known_datasets) != sum(len(product.datasets) for product in products):
        errors.append("Duplicate dataset URNs across governed data products")
    for urn in known_datasets:
        try:
            _, _, env = dataset_urn_parts(urn)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if env != ENV:
            errors.append(
                f"Dataset environment must be {ENV} for native OpenLineage identity: {urn}"
            )
    for product in products:
        if product.flow_id != pipeline_flow_id(product.id):
            errors.append(
                f"DataFlow identity mismatch for {product.id}: "
                f"{product.flow_id} != {pipeline_flow_id(product.id)}"
            )
    all_job_urns = {
        job_urn(flow_urn(product.flow_id), job.id)
        for product in products
        for job in product.jobs
    }
    if len(all_job_urns) != sum(len(product.jobs) for product in products):
        errors.append("Duplicate DataJob URNs across governed data products")

    contracts: dict[str, str] = {}
    assertion_urns: set[str] = set()
    for product in products:
        for dataset in product.datasets:
            if not dataset.schema:
                errors.append(f"Missing schema contract for {dataset.urn}")
            if not dataset.validation_pipeline:
                errors.append(f"Missing validation pipeline for {dataset.urn}")
            if not dataset.custom_properties.get("contract"):
                errors.append(f"Missing contract description for {dataset.urn}")
            assertion_urns.add(assertion_urn(dataset.urn, "schema"))
            assertion_urns.add(assertion_urn(dataset.urn, "data_quality"))
        contracts[product.id] = f"ACTIVE:{len(product.datasets)}-datasets"

    expected_assertions = len(known_datasets) * 2
    if len(assertion_urns) != expected_assertions:
        errors.append(
            "Assertion identity collision: "
            f"expected {expected_assertions}, found {len(assertion_urns)}"
        )

    if errors:
        raise RuntimeError(
            "Governance coverage verification failed: " + " | ".join(errors)
        )
    return {
        "contracts": contracts,
        "datasets": len(known_datasets),
        "jobs": len(all_job_urns),
        "runtime_lineage": {
            "mode": "native-openlineage",
            "endpoint": "/openapi/openlineage/api/v1/lineage",
        },
        "validation": {
            "mode": "datahub-custom-assertion-writeback",
            "intermediate_reports": False,
        },
        "verified": True,
    }


def emit_products(
    emitter: GovernanceEmitter,
    products: tuple[DataProduct, ...],
) -> tuple[dict[str, str], dict[str, Any]]:
    coverage = verify_governance_coverage(products)
    for tag, description, color in (
        (
            "DP1",
            "Data product DP1: Data Generator to optimized Bronze Iceberg lakehouse.",
            "#2E7D32",
        ),
        (
            "DP2",
            "Data product DP2: Bronze Iceberg to optimized curated Silver Iceberg.",
            "#1565C0",
        ),
        (
            "DP3",
            "Data product DP3: Silver Iceberg to Iceberg features and PostgreSQL Feast offline store.",
            "#6A1B9A",
        ),
        (
            "CDC_INGESTION",
            "PostgreSQL WAL captured by Debezium and published to Kafka.",
            "#EF6C00",
        ),
        (
            "STREAMING_FEATURES",
            "Continuous Flink processing into PostgreSQL and Redis feature stores.",
            "#00838F",
        ),
        (
            "DataContract",
            "Entity has an explicit schema or pipeline contract in the RecSys data platform repo.",
            "#455A64",
        ),
        (
            "NativePipeline",
            "Entity is produced by Spark, Flink, Debezium, Iceberg, or Redis native runtime.",
            "#00897B",
        ),
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
        resource_urns = (
            tuple(item.urn for item in product.datasets)
            + (flow,)
            + tuple(job_urn(flow, job.id) for job in product.jobs)
        )
        batch_set_data_product(emitter, product, product_urn, resource_urns)
    return product_urns, coverage


def emit_schemas(emitter: GovernanceEmitter, products: tuple[DataProduct, ...]) -> int:
    count = 0
    for product in products:
        for dataset in product.datasets:
            emit_aspect(
                emitter, dataset.urn, "schemaMetadata", schema_metadata(dataset)
            )
            if dataset.validation_pipeline:
                emit_aspect(
                    emitter,
                    assertion_urn(dataset.urn, "schema"),
                    "assertionInfo",
                    schema_assertion_info(dataset),
                )
            count += 1
    return count


def datahub_metric_samples(summary: dict[str, Any]) -> list[MetricSample]:
    ingested = 1.0 if summary.get("ingested") else 0.0
    samples = [
        MetricSample("recsys_datahub_ingest_success", ingested),
        MetricSample(
            "recsys_datahub_ingest_timestamp_seconds", float(int(time.time()))
        ),
        MetricSample(
            "recsys_datahub_ingest_dataset_count", float(summary.get("datasets", 0))
        ),
        MetricSample("recsys_datahub_ingest_job_count", float(summary.get("jobs", 0))),
        MetricSample(
            "recsys_datahub_ingest_data_product_count",
            float(len(summary.get("data_products", []))),
        ),
    ]
    for product in summary.get("data_products", []):
        samples.append(
            MetricSample(
                "recsys_datahub_ingest_data_product_present",
                1.0,
                {"data_product": str(product)},
            )
        )
    return samples


def push_datahub_ingest_metrics(
    summary: dict[str, Any], pushgateway_url: str | None
) -> None:
    push_metrics(
        datahub_metric_samples(summary),
        "recsys_datahub_governance",
        gateway_url=pushgateway_url,
        grouping_key={"gms_url": summary.get("gms_url", "unknown").replace("/", "_")},
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest RecSys batch, CDC, and streaming governance metadata into DataHub."
    )
    parser.add_argument(
        "--gms-url", default="http://localhost:8088", help="DataHub GMS base URL."
    )
    parser.add_argument(
        "--pushgateway-url",
        default=os.getenv("PUSHGATEWAY_URL", ""),
        help="Optional Pushgateway URL for ingest metrics.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=os.getenv("DATAHUB_INGEST_STRICT", "").lower() in {"1", "true", "yes"},
    )
    parser.add_argument(
        "--schemas-only",
        action="store_true",
        help="Refresh dataset schemas without re-emitting lineage or validation results.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify governance definitions and data-contract coverage without contacting DataHub.",
    )
    args = parser.parse_args()
    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())
    if args.verify_only:
        try:
            coverage = verify_governance_coverage(products)
        except Exception as exc:
            print(
                json.dumps(
                    {"verified": False, "error": str(exc)}, indent=2, sort_keys=True
                )
            )
            return 1
        print(json.dumps(coverage, indent=2, sort_keys=True))
        return 0
    emitter = GovernanceEmitter(args.gms_url)
    try:
        if args.schemas_only:
            dataset_count = emit_schemas(emitter, products)
            product_urns: dict[str, str] = {}
            coverage: dict[str, Any] = {"verified": False, "reason": "schemas-only"}
        else:
            product_urns, coverage = emit_products(emitter, products)
            dataset_count = sum(len(product.datasets) for product in products)
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
    finally:
        emitter.close()
    summary = {
        "data_products": [product.id for product in products],
        "data_product_entities": product_urns,
        "datasets": dataset_count,
        "jobs": sum(len(product.jobs) for product in products),
        "gms_url": args.gms_url,
        "ingested": True,
        "governance_coverage": coverage,
    }
    push_datahub_ingest_metrics(summary, args.pushgateway_url or None)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
