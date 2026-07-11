from __future__ import annotations

import argparse
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from lakehouse.iceberg import RAW_GENERATOR_TABLES, SILVER_LAKEHOUSE_TABLES
from metadata.governance_catalog import (
    BRONZE_URNS,
    ICEBERG_FEATURE_URNS,
    KAFKA_TOPIC_URNS,
    POSTGRES_FEATURE_URNS,
    REDIS_FEATURE_URNS,
    SILVER_URNS,
    SOURCE_POSTGRES_URNS,
    flow_urn,
    job_urn,
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
from validate.governance_contracts import read_report


ACTOR = "urn:li:corpuser:datahub"
GOVERNANCE_DOMAIN_NAME = "RecSys Data Platform"
GOVERNANCE_DOMAIN_DESCRIPTION = "Governed batch, CDC, and streaming pipelines for the RecSys data platform."
ASSERTION_NAMESPACE = uuid.UUID("5851f697-2fcb-4938-b5c8-34fcb1f9f297")


@dataclass(frozen=True)
class Dataset:
    urn: str
    name: str
    description: str
    tags: tuple[str, ...]
    custom_properties: dict[str, str]
    schema: tuple[SchemaColumn, ...]
    primary_keys: tuple[str, ...] = ()
    upstreams: tuple[str, ...] = ()
    validation_pipeline: str | None = None
    required_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    description: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    tags: tuple[str, ...]
    custom_properties: dict[str, str]
    input_jobs: tuple[str, ...] = ()


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


def contract_id(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-").lower()
    return slug[:180] or "recsys-data-contract"


def assertion_urn(dataset_urn_value: str, assertion_type: str = "quality") -> str:
    return f"urn:li:assertion:{uuid.uuid5(ASSERTION_NAMESPACE, f'{dataset_urn_value}:{assertion_type}')}"


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


def batch_set_data_product(
    emitter: DataHubEmitter,
    product: DataProduct,
    product_urn: str,
    resource_urns: tuple[str, ...],
) -> None:
    emitter.graphql(
        """
        mutation batchSetDataProduct($input: BatchSetDataProductInput!) {
          batchSetDataProduct(input: $input)
        }
        """,
        {"input": {"dataProductUrn": product_urn, "resourceUrns": list(resource_urns)}},
    )
    stamp = audit_stamp()
    emitter.emit(
        product_urn,
        "dataProduct",
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
    emitter.emit(dataset.urn, "dataset", "schemaMetadata", schema_metadata(dataset))
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
    if any(token in normalized for token in ("INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL")):
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
            "description": column.description or f"{column.name} field in {dataset.name}.",
            "type": {"type": {f"com.linkedin.schema.{_datahub_type(column.native_type)}": {}}},
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
        "platformSchema": {"com.linkedin.schema.OtherSchema": {"rawSchema": json.dumps(raw_schema, sort_keys=True)}},
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


def validation_result(dataset: Dataset) -> tuple[str, str, list[dict[str, Any]]]:
    try:
        report = read_report(dataset.validation_pipeline or "")
    except Exception as exc:
        return "ERROR", "unknown", [{"name": "validation_report", "status": "ERROR", "observed": str(exc)}]
    result = report.get("datasets", {}).get(dataset.urn)
    if not isinstance(result, dict):
        return "ERROR", str(report.get("run_id", "unknown")), [
            {
                "name": "validation_report",
                "status": "ERROR",
                "observed": f"Dataset {dataset.urn} missing from latest {dataset.validation_pipeline} report",
            }
        ]
    status = str(result.get("status", "ERROR"))
    if status not in {"SUCCESS", "FAILURE", "ERROR"}:
        status = "ERROR"
    return status, str(report.get("run_id", "unknown")), list(result.get("checks", []))


def _assertion_status(checks: list[dict[str, Any]], names: set[str], fallback: str) -> str:
    selected = [str(item.get("status", "ERROR")) for item in checks if item.get("name") in names]
    if not selected:
        return fallback
    return "ERROR" if "ERROR" in selected else "FAILURE" if "FAILURE" in selected else "SUCCESS"


def _emit_assertion(
    emitter: DataHubEmitter,
    dataset: Dataset,
    *,
    assertion_type: str,
    status: str,
    run_id: str,
    checks: list[dict[str, Any]],
) -> str:
    urn = assertion_urn(dataset.urn, assertion_type.lower())
    if assertion_type == "SCHEMA":
        emitter.emit(urn, "assertion", "assertionInfo", schema_assertion_info(dataset))
        assertion = urn
    else:
        assertion = emitter.graphql(
            """
            mutation upsertAssertion($urn: String, $input: UpsertCustomAssertionInput!) {
              upsertCustomAssertion(urn: $urn, input: $input) { urn }
            }
            """,
            {
                "urn": urn,
                "input": {
                    "entityUrn": dataset.urn,
                    "type": assertion_type,
                    "description": f"{assertion_type.replace('_', ' ').title()} validation for {dataset.name}",
                    "platform": {"urn": "urn:li:dataPlatform:datahub"},
                    "logic": json.dumps(
                        {
                            "pipeline": dataset.validation_pipeline,
                            "required_columns": list(dataset.required_columns),
                            "checks": checks,
                        },
                        sort_keys=True,
                    ),
                },
            },
        )["upsertCustomAssertion"]["urn"]
    emitter.graphql(
        """
        mutation reportAssertionResult($urn: String!, $result: AssertionResultInput!) {
          reportAssertionResult(urn: $urn, result: $result)
        }
        """,
        {
            "urn": assertion,
            "result": {
                "type": status,
                "timestampMillis": int(time.time() * 1000),
                "properties": [
                    {"key": "pipeline", "value": dataset.validation_pipeline or "unknown"},
                    {"key": "run_id", "value": run_id},
                    {"key": "observed_checks", "value": json.dumps(checks, sort_keys=True)},
                ],
            },
        },
    )
    return assertion


def emit_dataset_contract(emitter: DataHubEmitter, dataset: Dataset) -> None:
    contract_text = dataset.custom_properties.get("contract", "RecSys governed dataset contract")
    status, run_id, checks = validation_result(dataset)
    schema_status = _assertion_status(checks, {"required_columns", "schema"}, status)
    schema_assertion = _emit_assertion(
        emitter,
        dataset,
        assertion_type="SCHEMA",
        status=schema_status,
        run_id=run_id,
        checks=[item for item in checks if item.get("name") in {"required_columns", "schema"}],
    )
    quality_assertion = _emit_assertion(
        emitter,
        dataset,
        assertion_type="DATA_QUALITY",
        status=status,
        run_id=run_id,
        checks=checks,
    )
    emitter.graphql(
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
                "id": contract_id(f"{dataset.urn}-contract"),
            }
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
            "inputDatajobs": list(job.input_jobs),
        },
    )
    emitter.emit(urn, "dataJob", "globalTags", tag_associations(job.tags))


def _dataset(
    urn: str,
    name: str,
    description: str,
    product: str,
    contract: str,
    *,
    schema: tuple[SchemaColumn, ...],
    primary_keys: tuple[str, ...] = (),
    upstreams: tuple[str, ...] = (),
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
        upstreams=upstreams,
        validation_pipeline=validation_pipeline,
        required_columns=required_columns,
    )


def dp1() -> DataProduct:
    bronze = tuple(
        _dataset(
            BRONZE_URNS[table],
            f"recsys.lakehouse.bronze_{table}",
            "DP1 Bronze Parquet lakehouse table with source-run and ingestion metadata.",
            "DP1",
            "Non-empty Bronze table with source key, source_run_id, and lakehouse_ingestion_ts",
            schema=bronze_schema(table),
            primary_keys=("source_run_id",) + RAW_PRIMARY_KEYS[table],
            validation_pipeline="DP1",
            required_columns=("source_run_id", "lakehouse_ingestion_ts"),
        )
        for table in RAW_GENERATOR_TABLES
    )
    flow = flow_urn("recsys_dp1_raw_to_bronze")
    return DataProduct(
        id="DP1",
        flow_id="recsys_dp1_raw_to_bronze",
        flow_name="DP1 Data Generator Batch Ingestion To Bronze Lakehouse",
        description="Direct batch ingestion from Data Generator output into the Bronze Parquet lakehouse.",
        tags=("DP1", "DataContract", "NativePipeline"),
        datasets=bronze,
        jobs=(
            Job(
                id="ingest_stage",
                name="Ingest Stage - Data Generator Batch Ingestion",
                description="Runs the historical Data Generator in the batch pod and ingests its ephemeral output directly into the Bronze Parquet lakehouse.",
                inputs=(),
                outputs=tuple(item.urn for item in bronze),
                tags=("DP1", "DataContract", "NativePipeline"),
                custom_properties={"engine": "Data Generator plus PyArrow Parquet ingestion"},
            ),
            Job(
                id="validate_stage",
                name="Validate Stage",
                description="Validates Bronze table existence, row counts, source keys, and ingestion metadata.",
                inputs=tuple(item.urn for item in bronze),
                outputs=(),
                tags=("DP1", "DataContract", "NativePipeline"),
                custom_properties={"report": "governance/validation/dp1/latest.json"},
                input_jobs=(job_urn(flow, "ingest_stage"),),
            ),
        ),
    )


def dp2() -> DataProduct:
    upstreams = {
        "clean_behavior_events": (BRONZE_URNS["behavior_events"],),
        "rejected_behavior_events": (BRONZE_URNS["behavior_events"],),
        "clean_impressions": (BRONZE_URNS["impressions"],),
        "clean_recommendation_requests": (BRONZE_URNS["recommendation_requests"],),
        "order_facts": (BRONZE_URNS["orders"], BRONZE_URNS["order_items"]),
        "product_scd": (BRONZE_URNS["product_snapshots"], BRONZE_URNS["products"]),
        "users": (BRONZE_URNS["users"],),
        "products": (BRONZE_URNS["products"],),
        "user_preferences": (BRONZE_URNS["user_preferences"],),
    }
    silver = tuple(
        _dataset(
            SILVER_URNS[table],
            f"iceberg.recsys.lakehouse.silver_{table}",
            "DP2 curated Silver Iceberg table produced from Bronze Parquet inputs.",
            "DP2",
            "Readable Silver Iceberg table; clean_behavior_events must be unique by event_id",
            schema=silver_schema(table),
            primary_keys=SILVER_PRIMARY_KEYS[table],
            upstreams=upstreams[table],
            validation_pipeline="DP2",
            required_columns=("event_id", "event_timestamp", "ingestion_ts") if table == "clean_behavior_events" else (),
        )
        for table in SILVER_LAKEHOUSE_TABLES
    )
    flow = flow_urn("recsys_dp2_bronze_to_silver_gold")
    return DataProduct(
        id="DP2",
        flow_id="recsys_dp2_bronze_to_silver_gold",
        flow_name="DP2 Bronze To Silver And Gold",
        description="PySpark curation from Bronze Parquet tables into deduplicated and normalized Silver Iceberg tables.",
        tags=("DP2", "DataContract", "NativePipeline"),
        datasets=silver,
        jobs=(
            Job(
                id="ingest_stage",
                name="Ingest Stage",
                description="Reads Bronze Parquet, normalizes schemas, deduplicates events, and writes Silver Iceberg tables.",
                inputs=tuple(BRONZE_URNS.values()),
                outputs=tuple(item.urn for item in silver),
                tags=("DP2", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PySpark plus Iceberg"},
            ),
            Job(
                id="validate_stage",
                name="Validate Stage",
                description="Validates Silver outputs and confirms clean behavior events contain no duplicate event_id values.",
                inputs=tuple(item.urn for item in silver),
                outputs=(),
                tags=("DP2", "DataContract", "NativePipeline"),
                custom_properties={"report": "governance/validation/dp2/latest.json"},
                input_jobs=(job_urn(flow, "ingest_stage"),),
            ),
        ),
    )


def dp3() -> DataProduct:
    iceberg_upstreams = {
        "user_sequence_features": (SILVER_URNS["clean_behavior_events"],),
        "user_aggregate_features": (SILVER_URNS["clean_behavior_events"],),
        "item_features": (SILVER_URNS["clean_behavior_events"], SILVER_URNS["product_scd"]),
        "ml_ranking_labels": (SILVER_URNS["clean_impressions"], SILVER_URNS["clean_behavior_events"]),
        "ml_bst_training": (
            ICEBERG_FEATURE_URNS["user_sequence_features"],
            ICEBERG_FEATURE_URNS["user_aggregate_features"],
            ICEBERG_FEATURE_URNS["item_features"],
            ICEBERG_FEATURE_URNS["ml_ranking_labels"],
        ),
    }
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
            upstreams=iceberg_upstreams[table],
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
            upstreams=(ICEBERG_FEATURE_URNS[table],),
            validation_pipeline="DP3",
            required_columns=required[table],
        )
        for table in POSTGRES_FEATURE_URNS
    )
    flow = flow_urn("recsys_dp3_offline_feature_table")
    return DataProduct(
        id="DP3",
        flow_id="recsys_dp3_offline_feature_table",
        flow_name="DP3 Silver To Feast Offline Features",
        description="PySpark feature engineering from DP2 Silver Iceberg tables into Iceberg features and PostgreSQL Feast offline tables.",
        tags=("DP3", "DataContract", "NativePipeline"),
        datasets=iceberg + postgres,
        jobs=(
            Job(
                id="ingest_stage",
                name="Ingest Stage",
                description="Reads DP2 Silver tables, computes offline features, writes Iceberg outputs, and exports PostgreSQL Feast tables.",
                inputs=tuple(SILVER_URNS.values()),
                outputs=tuple(item.urn for item in iceberg + postgres),
                tags=("DP3", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PySpark, Iceberg, and PostgreSQL"},
            ),
            Job(
                id="validate_stage",
                name="Validate Stage",
                description="Validates both Iceberg feature outputs and PostgreSQL Feast offline-store tables.",
                inputs=tuple(item.urn for item in iceberg + postgres),
                outputs=(),
                tags=("DP3", "DataContract", "NativePipeline"),
                custom_properties={"report": "governance/validation/dp3/latest.json"},
                input_jobs=(job_urn(flow, "ingest_stage"),),
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
            upstreams=(SOURCE_POSTGRES_URNS[table],),
        )
        for table in RAW_GENERATOR_TABLES
    )
    return DataProduct(
        id="CDC_INGESTION",
        flow_id="recsys_cdc_postgres_to_kafka",
        flow_name="CDC PostgreSQL To Kafka",
        description="PostgreSQL WAL captured by Debezium and published to cdc.* Kafka topics.",
        tags=("CDC_INGESTION", "DataContract", "NativePipeline"),
        datasets=source + topics,
        jobs=(
            Job(
                id="register_debezium_connector",
                name="Register Debezium Connector",
                description="Registers the Debezium connector linking source PostgreSQL tables to Kafka topics.",
                inputs=tuple(item.urn for item in source),
                outputs=tuple(item.urn for item in topics),
                tags=("CDC_INGESTION", "DataContract", "NativePipeline"),
                custom_properties={"contract": "ingest.postgres_cdc_contracts.SOURCE_TABLE_CONTRACTS"},
            ),
        ),
    )


def streaming_features() -> DataProduct:
    topic = KAFKA_TOPIC_URNS["behavior_events"]
    redis = tuple(
        _dataset(
            REDIS_FEATURE_URNS[table],
            f"redis_online.{table}",
            "Redis online feature keys updated continuously by the PyFlink online-store job.",
            "STREAMING_FEATURES",
            "Redis online feature entity key and TTL contract",
            schema=feature_schema(table),
            primary_keys=FEATURE_PRIMARY_KEYS[table][:1],
            upstreams=(topic,),
        )
        for table in REDIS_FEATURE_URNS
    )
    return DataProduct(
        id="STREAMING_FEATURES",
        flow_id="recsys_flink_stream_features",
        flow_name="Flink Streaming Features",
        description="Two continuously running Flink jobs consume cdc.behavior_events and update PostgreSQL offline plus Redis online features.",
        tags=("STREAMING_FEATURES", "DataContract", "NativePipeline"),
        datasets=redis,
        jobs=(
            Job(
                id="run_flink_stream_to_offline_store",
                name="Run Flink Stream To Offline Store",
                description="Consumes behavior CDC events and updates PostgreSQL Feast offline feature tables.",
                inputs=(topic,),
                outputs=tuple(POSTGRES_FEATURE_URNS[table] for table in REDIS_FEATURE_URNS),
                tags=("STREAMING_FEATURES", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PyFlink DataStream plus PostgreSQL sink"},
            ),
            Job(
                id="run_flink_stream_to_online_store",
                name="Run Flink Stream To Online Store",
                description="Consumes behavior CDC events and updates Redis online feature keys.",
                inputs=(topic,),
                outputs=tuple(item.urn for item in redis),
                tags=("STREAMING_FEATURES", "DataContract", "NativePipeline"),
                custom_properties={"engine": "PyFlink DataStream plus Redis sink"},
            ),
        ),
    )


def emit_products(emitter: DataHubEmitter, products: tuple[DataProduct, ...]) -> dict[str, str]:
    for tag, description, color in (
        ("DP1", "Data product DP1: Data Generator raw S3 to Bronze Parquet lakehouse.", "#2E7D32"),
        ("DP2", "Data product DP2: Bronze Parquet to curated Silver Iceberg.", "#1565C0"),
        ("DP3", "Data product DP3: Silver Iceberg to Iceberg features and PostgreSQL Feast offline store.", "#6A1B9A"),
        ("CDC_INGESTION", "PostgreSQL WAL captured by Debezium and published to Kafka.", "#EF6C00"),
        ("STREAMING_FEATURES", "Continuous Flink processing into PostgreSQL and Redis feature stores.", "#00838F"),
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
        batch_set_data_product(emitter, product, product_urn, resource_urns)
    return product_urns


def emit_schemas(emitter: DataHubEmitter, products: tuple[DataProduct, ...]) -> int:
    count = 0
    for product in products:
        for dataset in product.datasets:
            emitter.emit(dataset.urn, "dataset", "schemaMetadata", schema_metadata(dataset))
            if dataset.validation_pipeline:
                emitter.emit(
                    assertion_urn(dataset.urn, "schema"),
                    "assertion",
                    "assertionInfo",
                    schema_assertion_info(dataset),
                )
            count += 1
    return count


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
    parser = argparse.ArgumentParser(description="Ingest RecSys batch, CDC, and streaming governance metadata into DataHub.")
    parser.add_argument("--gms-url", default="http://localhost:8088", help="DataHub GMS base URL.")
    parser.add_argument("--pushgateway-url", default=os.getenv("PUSHGATEWAY_URL", ""), help="Optional Pushgateway URL for ingest metrics.")
    parser.add_argument("--strict", action="store_true", default=os.getenv("DATAHUB_INGEST_STRICT", "").lower() in {"1", "true", "yes"})
    parser.add_argument("--schemas-only", action="store_true", help="Refresh dataset schemas without re-emitting lineage or validation results.")
    args = parser.parse_args()
    products = (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())
    emitter = DataHubEmitter(args.gms_url)
    try:
        if args.schemas_only:
            dataset_count = emit_schemas(emitter, products)
            product_urns: dict[str, str] = {}
        else:
            product_urns = emit_products(emitter, products)
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
    summary = {
        "data_products": [product.id for product in products],
        "data_product_entities": product_urns,
        "datasets": dataset_count,
        "jobs": sum(len(product.jobs) for product in products),
        "gms_url": args.gms_url,
        "ingested": True,
    }
    push_datahub_ingest_metrics(summary, args.pushgateway_url or None)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
