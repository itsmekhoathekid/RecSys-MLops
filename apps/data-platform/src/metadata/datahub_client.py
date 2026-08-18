from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    ArrayTypeClass,
    AuditStampClass,
    BooleanTypeClass,
    BytesTypeClass,
    DateTypeClass,
    NumberTypeClass,
    OtherSchemaClass,
    RecordTypeClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
    TimeTypeClass,
    UpstreamLineageClass,
)
from datahub.sdk import DataHubClient, Dataset, Tag

from metadata.governance_catalog import (
    DataProductSpec,
    DatasetSpec,
    dataset_urn_parts,
    validate_catalog,
)

ACTOR = "urn:li:corpuser:datahub"
DOMAIN_NAME = "RecSys Data Platform"
DOMAIN_DESCRIPTION = "Static batch dataset catalog and lineage for the RecSys data platform."


@dataclass(frozen=True)
class SyncSummary:
    data_products: int
    datasets: int
    lineage_edges: int


@dataclass(frozen=True)
class RemoteVerification:
    data_products: int
    datasets: int
    lineage_edges: int
    verified: bool


class DataHubCatalogClient:
    """High-level DataHub SDK adapter for the static dataset catalog."""

    def __init__(self, graph: DataHubGraph) -> None:
        self._graph = graph
        self._client = DataHubClient(graph=graph)

    @classmethod
    def from_env(cls, gms_url: str | None = None) -> "DataHubCatalogClient":
        token = (os.getenv("DATAHUB_TOKEN") or os.getenv("DATAHUB_GMS_TOKEN") or "").strip()
        config = DatahubClientConfig(
            server=(gms_url or os.getenv("DATAHUB_GMS_URL") or "http://localhost:8088").rstrip("/"),
            token=token or None,
            timeout_sec=180,
            retry_status_codes=[408, 425, 429, *range(500, 600)],
            retry_max_times=5,
            datahub_component="recsys-static-catalog",
        )
        return cls(DataHubGraph(config))

    @staticmethod
    def _expected_edges(products: tuple[DataProductSpec, ...]) -> set[tuple[str, str]]:
        return {
            (upstream, dataset.urn)
            for product in products
            for dataset in product.datasets
            for upstream in dataset.upstreams
        }

    @staticmethod
    def _field_type(native_type: str) -> SchemaFieldDataTypeClass:
        value = native_type.upper()
        if "ARRAY" in value or value.endswith("[]"):
            kind = ArrayTypeClass()
        elif any(token in value for token in ("STRUCT", "RECORD", "MAP", "JSON")):
            kind = RecordTypeClass()
        elif "DATE" in value and "TIME" not in value:
            kind = DateTypeClass()
        elif any(token in value for token in ("TIME", "TIMESTAMP")):
            kind = TimeTypeClass()
        elif "BOOL" in value:
            kind = BooleanTypeClass()
        elif any(token in value for token in ("INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL")):
            kind = NumberTypeClass()
        elif any(token in value for token in ("BINARY", "BYTES", "BLOB")):
            kind = BytesTypeClass()
        else:
            kind = StringTypeClass()
        return SchemaFieldDataTypeClass(type=kind)

    @classmethod
    def _schema_metadata(cls, spec: DatasetSpec) -> SchemaMetadataClass:
        platform, _, _ = dataset_urn_parts(spec.urn)
        stamp = AuditStampClass(time=int(time.time() * 1000), actor=ACTOR)
        raw_schema = {
            "name": spec.name,
            "type": "record",
            "fields": [
                {"name": item.name, "type": item.native_type, "nullable": item.nullable}
                for item in spec.schema
            ],
        }
        return SchemaMetadataClass(
            schemaName=spec.name,
            platform=f"urn:li:dataPlatform:{platform}",
            version=0,
            hash="",
            platformSchema=OtherSchemaClass(rawSchema=json.dumps(raw_schema, sort_keys=True)),
            fields=[
                SchemaFieldClass(
                    fieldPath=item.name,
                    type=cls._field_type(item.native_type),
                    nativeDataType=item.native_type,
                    nullable=item.nullable,
                    description=item.description or f"{item.name} field in {spec.name}.",
                    recursive=False,
                    isPartOfKey=item.name in spec.primary_keys,
                    lastModified=stamp,
                )
                for item in spec.schema
            ],
            primaryKeys=list(spec.primary_keys),
            created=stamp,
            lastModified=stamp,
        )

    def _find_entity(self, entity_type: str, name: str) -> dict | None:
        result = self._graph.execute_graphql(
            """
            query searchEntity($input: SearchAcrossEntitiesInput!) {
              searchAcrossEntities(input: $input) {
                searchResults { entity { urn type ... on Domain { properties { name } }
                  ... on DataProduct { properties { name } domain { domain { urn } } } } }
              }
            }
            """,
            {"input": {"types": [entity_type], "query": name, "start": 0, "count": 50}},
        )
        for item in result.get("searchAcrossEntities", {}).get("searchResults", []):
            entity = item.get("entity") or {}
            if (entity.get("properties") or {}).get("name") == name:
                return entity
        return None

    def _ensure_domain(self) -> str:
        existing = self._find_entity("DOMAIN", DOMAIN_NAME)
        if existing:
            return str(existing["urn"])
        result = self._graph.execute_graphql(
            "mutation createDomain($input: CreateDomainInput!) { createDomain(input: $input) }",
            {"input": {"name": DOMAIN_NAME, "description": DOMAIN_DESCRIPTION}},
        )
        return str(result["createDomain"])

    def _upsert_tags(self, products: tuple[DataProductSpec, ...]) -> None:
        tags = sorted({tag for product in products for tag in product.tags} | {
            tag for product in products for dataset in product.datasets for tag in dataset.tags
        })
        for tag in tags:
            self._client.entities.upsert(Tag(
                name=tag,
                display_name=tag,
                description=f"RecSys static catalog tag: {tag}",
            ))

    def _upsert_dataset(self, spec: DatasetSpec, domain_urn: str) -> None:
        platform, name, env = dataset_urn_parts(spec.urn)
        self._client.entities.upsert(Dataset(
            platform=platform,
            name=name,
            env=env,
            display_name=spec.name,
            description=spec.description,
            custom_properties=dict(spec.custom_properties),
            tags=[f"urn:li:tag:{tag}" for tag in spec.tags],
            domain=domain_urn,
            schema=self._schema_metadata(spec),
            upstreams=list(spec.upstreams),
        ))

    def _upsert_data_product(
        self, product: DataProductSpec, domain_urn: str, dataset_urns: tuple[str, ...]
    ) -> str:
        # Earlier catalog versions used the stable product id as the display
        # name. Resolve both forms so the first static-catalog sync updates the
        # existing entity instead of creating a duplicate.
        existing = self._find_entity("DATA_PRODUCT", product.name) or self._find_entity(
            "DATA_PRODUCT", product.id
        )
        if existing and (existing.get("domain") or {}).get("domain", {}).get("urn") == domain_urn:
            urn = str(existing["urn"])
            self._graph.execute_graphql(
                "mutation updateDataProduct($urn: String!, $input: UpdateDataProductInput!) "
                "{ updateDataProduct(urn: $urn, input: $input) { urn } }",
                {"urn": urn, "input": {"name": product.name, "description": product.description}},
            )
        else:
            result = self._graph.execute_graphql(
                "mutation createDataProduct($input: CreateDataProductInput!) "
                "{ createDataProduct(input: $input) { urn } }",
                {"input": {"domainUrn": domain_urn, "properties": {
                    "name": product.name, "description": product.description,
                }}},
            )
            urn = str(result["createDataProduct"]["urn"])
        self._graph.execute_graphql(
            "mutation batchSetDataProduct($input: BatchSetDataProductInput!) "
            "{ batchSetDataProduct(input: $input) }",
            {"input": {"dataProductUrn": urn, "resourceUrns": list(dataset_urns)}},
        )
        return urn

    def sync(self, products: tuple[DataProductSpec, ...]) -> SyncSummary:
        coverage = validate_catalog(products)
        domain_urn = self._ensure_domain()
        self._upsert_tags(products)
        for product in products:
            for dataset in product.datasets:
                self._upsert_dataset(dataset, domain_urn)
            self._upsert_data_product(
                product, domain_urn, tuple(dataset.urn for dataset in product.datasets)
            )
        return SyncSummary(
            data_products=int(coverage["data_products"]),
            datasets=int(coverage["datasets"]),
            lineage_edges=int(coverage["lineage_edges"]),
        )

    def verify_remote(self, products: tuple[DataProductSpec, ...]) -> RemoteVerification:
        expected_edges = self._expected_edges(products)
        observed_edges: set[tuple[str, str]] = set()
        errors: list[str] = []
        for product in products:
            if (
                self._find_entity("DATA_PRODUCT", product.name) is None
                and self._find_entity("DATA_PRODUCT", product.id) is None
            ):
                errors.append(f"Missing Data Product: {product.id}")
            for dataset in product.datasets:
                if not self._graph.exists(dataset.urn):
                    errors.append(f"Missing dataset: {dataset.urn}")
                    continue
                aspect = self._graph.get_aspect(dataset.urn, UpstreamLineageClass)
                for upstream in (aspect.upstreams if aspect else []):
                    observed_edges.add((str(upstream.dataset), dataset.urn))
        if observed_edges != expected_edges:
            errors.append(
                f"Lineage mismatch: missing={sorted(expected_edges - observed_edges)}, "
                f"unexpected={sorted(observed_edges - expected_edges)}"
            )
        if errors:
            raise RuntimeError("Remote catalog verification failed: " + " | ".join(errors))
        return RemoteVerification(
            data_products=len(products),
            datasets=sum(len(product.datasets) for product in products),
            lineage_edges=len(observed_edges),
            verified=True,
        )

    def close(self) -> None:
        self._graph.close()
