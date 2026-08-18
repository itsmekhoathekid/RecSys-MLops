from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import (
    ArrayTypeClass,
    AuditStampClass,
    BooleanTypeClass,
    BytesTypeClass,
    DateTypeClass,
    DataContractPropertiesClass,
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
    assertion_urn,
    catalog_products,
    fallback_contract_urn,
    dataset_urn_parts,
    validate_catalog,
)
from validate.report_io import ValidationReport

ACTOR = "urn:li:corpuser:datahub"
DOMAIN_NAME = "RecSys Data Platform"
DOMAIN_DESCRIPTION = (
    "Static batch dataset catalog and lineage for the RecSys data platform."
)


@dataclass(frozen=True)
class SyncSummary:
    data_products: int
    datasets: int
    lineage_edges: int
    assertions: int
    data_contracts: int


@dataclass(frozen=True)
class RemoteVerification:
    data_products: int
    datasets: int
    lineage_edges: int
    assertions: int
    data_contracts: int
    assertions_with_results: int
    verified: bool


@dataclass(frozen=True)
class ValidationPublishSummary:
    product: str
    success: int
    failure: int
    error: int


class DataHubCatalogClient:
    """High-level DataHub SDK adapter for the static dataset catalog."""

    def __init__(self, graph: DataHubGraph) -> None:
        self._graph = graph
        self._client = DataHubClient(graph=graph)

    @classmethod
    def from_env(cls, gms_url: str | None = None) -> "DataHubCatalogClient":
        token = (
            os.getenv("DATAHUB_TOKEN") or os.getenv("DATAHUB_GMS_TOKEN") or ""
        ).strip()
        config = DatahubClientConfig(
            server=(
                gms_url or os.getenv("DATAHUB_GMS_URL") or "http://localhost:8088"
            ).rstrip("/"),
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
        elif any(
            token in value
            for token in ("INT", "DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL")
        ):
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
            platformSchema=OtherSchemaClass(
                rawSchema=json.dumps(raw_schema, sort_keys=True)
            ),
            fields=[
                SchemaFieldClass(
                    fieldPath=item.name,
                    type=cls._field_type(item.native_type),
                    nativeDataType=item.native_type,
                    nullable=item.nullable,
                    description=item.description
                    or f"{item.name} field in {spec.name}.",
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
        tags = sorted(
            {tag for product in products for tag in product.tags}
            | {
                tag
                for product in products
                for dataset in product.datasets
                for tag in dataset.tags
            }
        )
        for tag in tags:
            self._client.entities.upsert(
                Tag(
                    name=tag,
                    display_name=tag,
                    description=f"RecSys static catalog tag: {tag}",
                )
            )

    def _upsert_dataset(self, spec: DatasetSpec, domain_urn: str) -> None:
        platform, name, env = dataset_urn_parts(spec.urn)
        self._client.entities.upsert(
            Dataset(
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
            )
        )

    def _upsert_data_product(
        self, product: DataProductSpec, domain_urn: str, dataset_urns: tuple[str, ...]
    ) -> str:
        # Earlier catalog versions used the stable product id as the display
        # name. Resolve both forms so the first static-catalog sync updates the
        # existing entity instead of creating a duplicate.
        existing = self._find_entity("DATA_PRODUCT", product.name) or self._find_entity(
            "DATA_PRODUCT", product.id
        )
        if (
            existing
            and (existing.get("domain") or {}).get("domain", {}).get("urn")
            == domain_urn
        ):
            urn = str(existing["urn"])
            self._graph.execute_graphql(
                "mutation updateDataProduct($urn: String!, $input: UpdateDataProductInput!) "
                "{ updateDataProduct(urn: $urn, input: $input) { urn } }",
                {
                    "urn": urn,
                    "input": {"name": product.name, "description": product.description},
                },
            )
        else:
            result = self._graph.execute_graphql(
                "mutation createDataProduct($input: CreateDataProductInput!) "
                "{ createDataProduct(input: $input) { urn } }",
                {
                    "input": {
                        "domainUrn": domain_urn,
                        "properties": {
                            "name": product.name,
                            "description": product.description,
                        },
                    }
                },
            )
            urn = str(result["createDataProduct"]["urn"])
        self._graph.execute_graphql(
            "mutation batchSetDataProduct($input: BatchSetDataProductInput!) "
            "{ batchSetDataProduct(input: $input) }",
            {"input": {"dataProductUrn": urn, "resourceUrns": list(dataset_urns)}},
        )
        return urn

    def _dataset_contract(self, dataset_urn: str) -> str | None:
        result = self._graph.execute_graphql(
            "query datasetContract($urn: String!) { dataset(urn: $urn) "
            "{ contract { urn } } }",
            {"urn": dataset_urn},
        )
        contract = (result.get("dataset") or {}).get("contract") or {}
        if contract.get("urn"):
            return str(contract["urn"])
        # The previous governance emitter used a sanitized Dataset URN as its
        # contract id. A removed contract is not returned by dataset.contract,
        # so probe that deterministic legacy URN and reactivate it in place.
        legacy_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{dataset_urn}-contract").strip(
            "-"
        )
        legacy_urn = f"urn:li:dataContract:{legacy_id.lower()[:180]}"
        return legacy_urn if self._graph.exists(legacy_urn) else None

    def _upsert_custom_assertion(self, spec: DatasetSpec) -> str:
        urn = assertion_urn(spec.urn)
        self._graph.upsert_custom_assertion(
            urn=urn,
            entity_urn=spec.urn,
            type="RecSys Dataset Contract",
            description=spec.contract.description,
            platform_name="RecSys Local Validation",
        )
        self._graph.set_soft_delete_status(urn, delete=False)
        return urn

    def _upsert_data_contract(self, spec: DatasetSpec, assertion: str) -> str:
        contract_urn = self._dataset_contract(spec.urn) or fallback_contract_urn(
            spec.urn
        )
        # Keep the mutation inline so it remains compatible with the GraphQL
        # input type name exposed by both DataHub Core 1.6 and 1.7.
        mutation = f"""
        mutation upsertDataContract {{
          upsertDataContract(
            urn: {json.dumps(contract_urn)}
            input: {{
              entityUrn: {json.dumps(spec.urn)}
              freshness: []
              schema: []
              dataQuality: [{{assertionUrn: {json.dumps(assertion)}}}]
            }}
          ) {{ urn }}
        }}
        """
        result = self._graph.execute_graphql(mutation)
        resolved = (result.get("upsertDataContract") or {}).get("urn")
        if resolved and str(resolved) != contract_urn:
            raise RuntimeError(
                f"DataHub returned unexpected Data Contract URN for {spec.urn}: {resolved}"
            )
        self._graph.set_soft_delete_status(contract_urn, delete=False)
        return contract_urn

    def _assertion_details(self, urn: str) -> dict:
        result = self._graph.execute_graphql(
            """
            query assertionDetails($urn: String!) {
              assertion(urn: $urn) {
                urn
                info { type customAssertion { entityUrn } }
                runEvents(status: COMPLETE, limit: 1) {
                  runEvents { result { type } }
                }
              }
            }
            """,
            {"urn": urn},
        )
        return result.get("assertion") or {}

    def _latest_assertion_result(self, urn: str) -> str | None:
        details = self._assertion_details(urn)
        events = (details.get("runEvents") or {}).get("runEvents") or []
        if not events:
            return None
        return str((events[0].get("result") or {}).get("type") or "") or None

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
        for product in products:
            for dataset in product.datasets:
                assertion = self._upsert_custom_assertion(dataset)
                self._upsert_data_contract(dataset, assertion)
        return SyncSummary(
            data_products=int(coverage["data_products"]),
            datasets=int(coverage["datasets"]),
            lineage_edges=int(coverage["lineage_edges"]),
            assertions=int(coverage["assertions"]),
            data_contracts=int(coverage["data_contracts"]),
        )

    def verify_remote(
        self,
        products: tuple[DataProductSpec, ...],
        *,
        require_results: bool = False,
    ) -> RemoteVerification:
        expected_edges = self._expected_edges(products)
        observed_edges: set[tuple[str, str]] = set()
        errors: list[str] = []
        assertion_count = 0
        contract_count = 0
        assertions_with_results = 0
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
                for upstream in aspect.upstreams if aspect else []:
                    observed_edges.add((str(upstream.dataset), dataset.urn))
                expected_assertion = assertion_urn(dataset.urn)
                details = self._assertion_details(expected_assertion)
                info = details.get("info") or {}
                custom = info.get("customAssertion") or {}
                if (
                    info.get("type") != "CUSTOM"
                    or custom.get("entityUrn") != dataset.urn
                ):
                    errors.append(
                        f"Missing or invalid CUSTOM assertion: {expected_assertion}"
                    )
                else:
                    assertion_count += 1
                latest = self._latest_assertion_result(expected_assertion)
                if latest:
                    assertions_with_results += 1
                elif require_results:
                    errors.append(
                        f"Assertion has no completed result: {expected_assertion}"
                    )
                contract_urn = self._dataset_contract(dataset.urn)
                if not contract_urn:
                    errors.append(f"Missing Data Contract: {dataset.urn}")
                    continue
                properties = self._graph.get_aspect(
                    contract_urn, DataContractPropertiesClass
                )
                contract_assertions = {
                    str(item.assertion)
                    for item in ((properties.dataQuality or []) if properties else [])
                }
                if properties is None or str(properties.entity) != dataset.urn:
                    errors.append(f"Invalid Data Contract entity: {contract_urn}")
                elif contract_assertions != {expected_assertion}:
                    errors.append(
                        f"Data Contract assertion mismatch for {contract_urn}: "
                        f"{sorted(contract_assertions)}"
                    )
                else:
                    contract_count += 1
        if observed_edges != expected_edges:
            errors.append(
                f"Lineage mismatch: missing={sorted(expected_edges - observed_edges)}, "
                f"unexpected={sorted(observed_edges - expected_edges)}"
            )
        if errors:
            raise RuntimeError(
                "Remote catalog verification failed: " + " | ".join(errors)
            )
        return RemoteVerification(
            data_products=len(products),
            datasets=sum(len(product.datasets) for product in products),
            lineage_edges=len(observed_edges),
            assertions=assertion_count,
            data_contracts=contract_count,
            assertions_with_results=assertions_with_results,
            verified=True,
        )

    def publish_validation_reports(
        self,
        product_id: str,
        reports: tuple[ValidationReport, ...],
        expected_dataset_keys: tuple[str, ...],
    ) -> ValidationPublishSummary:
        products = {product.id: product for product in catalog_products()}
        if product_id not in products:
            raise ValueError(f"Unknown Data Product: {product_id}")
        product = products[product_id]
        datasets = {
            dataset.contract.validation_key: dataset for dataset in product.datasets
        }
        expected = tuple(dict.fromkeys(expected_dataset_keys))
        if len(expected) != len(expected_dataset_keys):
            raise ValueError("Expected dataset keys must be unique")
        unknown_expected = sorted(set(expected).difference(datasets))
        if unknown_expected:
            raise ValueError(f"Unknown expected dataset keys: {unknown_expected}")
        observed: dict[str, tuple[object, ValidationReport]] = {}
        for report in reports:
            if report.product_id != product_id:
                raise ValueError(
                    f"Report product mismatch: expected {product_id}, got {report.product_id}"
                )
            for result in report.datasets:
                if result.dataset_key not in datasets:
                    raise ValueError(
                        f"Unknown validation dataset key: {result.dataset_key}"
                    )
                if result.dataset_key not in expected:
                    raise ValueError(
                        f"Unexpected validation dataset key for this publish: "
                        f"{result.dataset_key}"
                    )
                if result.status not in {"SUCCESS", "FAILURE", "ERROR"}:
                    raise ValueError(
                        f"Unsupported validation status for {result.dataset_key}: "
                        f"{result.status}"
                    )
                if result.dataset_key in observed:
                    raise ValueError(
                        f"Duplicate validation dataset key: {result.dataset_key}"
                    )
                observed[result.dataset_key] = (result, report)
        counts = {"SUCCESS": 0, "FAILURE": 0, "ERROR": 0}
        now = int(time.time() * 1000)
        for key in expected:
            dataset = datasets[key]
            if key in observed:
                result, report = observed[key]
                status = result.status
                checks = result.checks
                run_id = report.run_id
                report_uri = report.report_uri
                error_message = (
                    "Validation execution returned ERROR" if status == "ERROR" else None
                )
            else:
                status = "ERROR"
                checks = ()
                run_id = "missing-report"
                report_uri = ""
                error_message = f"No validation report supplied for {key}"
            passed = sum(1 for item in checks if item.get("status") == "SUCCESS")
            failed = sum(1 for item in checks if item.get("status") == "FAILURE")
            properties = [
                {"key": "product", "value": product_id},
                {"key": "dataset_key", "value": key},
                {"key": "run_id", "value": run_id},
                {"key": "report_uri", "value": report_uri},
                {"key": "check_count", "value": str(len(checks))},
                {"key": "passed_checks", "value": str(passed)},
                {"key": "failed_checks", "value": str(failed)},
            ]
            self._graph.report_assertion_result(
                urn=assertion_urn(dataset.urn),
                timestamp_millis=now,
                type=status,
                properties=properties,
                error_type="UNKNOWN_ERROR" if status == "ERROR" else None,
                error_message=error_message,
            )
            counts[status] += 1
        return ValidationPublishSummary(
            product=product_id,
            success=counts["SUCCESS"],
            failure=counts["FAILURE"],
            error=counts["ERROR"],
        )

    def close(self) -> None:
        self._graph.close()
