#!/usr/bin/env python3
"""Preview, apply, or restore the static-dataset-lineage DataHub cutover."""

from __future__ import annotations

import argparse
import json
import os
import re
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig
from datahub.metadata.schema_classes import StatusClass, UpstreamLineageClass
from datahub.sdk import DataHubClient

from lakehouse.iceberg import RAW_GENERATOR_TABLES
from metadata.governance_catalog import catalog_products, dataset_urn, validate_catalog

DEFAULT_MANIFEST = Path(".ci-deploy/datahub-dataset-lineage-cutover.json")
ASSERTION_NAMESPACE = uuid.UUID("5851f697-2fcb-4938-b5c8-34fcb1f9f297")
AIRFLOW_DAGS = (
    "recsys_dp1_raw_to_bronze",
    "recsys_dp2_bronze_to_silver_gold",
    "recsys_dp3_offline_feature_table",
    "recsys_analytics_daily",
    "recsys_feature_drift_monitoring",
    "recsys_feast_materialize",
    "recsys_rag_item_index",
    "recsys_rag_item_reconciliation",
)


def known_flow_urns() -> tuple[str, ...]:
    airflow = tuple(f"urn:li:dataFlow:(airflow,{dag},PROD)" for dag in AIRFLOW_DAGS)
    return airflow + (
        "urn:li:dataFlow:(kafka-connect,recsys_cdc_postgres_to_kafka,PROD)",
        "urn:li:dataFlow:(flink,recsys_flink_stream_features,PROD)",
        "urn:li:dataFlow:(airflow,recsys_cdc_postgres_to_kafka,PROD)",
        "urn:li:dataFlow:(airflow,recsys_flink_stream_features,PROD)",
    )


def _relationships(graph: DataHubGraph, urn: str, entity_type: str) -> list[str]:
    result = graph.execute_graphql(
        """
        query relationships($urn: String!) {
          entity(urn: $urn) {
            relationships(input: { types: [], direction: INCOMING, start: 0, count: 1000 }) {
              relationships { entity { urn type } }
            }
          }
        }
        """,
        {"urn": urn},
    )
    items = ((result.get("entity") or {}).get("relationships") or {}).get("relationships", [])
    return sorted({
        item["entity"]["urn"] for item in items
        if (item.get("entity") or {}).get("type") == entity_type
    })


def related_jobs(graph: DataHubGraph, flow_urn: str) -> list[str]:
    return _relationships(graph, flow_urn, "DATA_JOB")


def related_process_instances(graph: DataHubGraph, job_urn: str) -> list[str]:
    return _relationships(graph, job_urn, "DATA_PROCESS_INSTANCE")


def legacy_dataset_urns() -> tuple[str, ...]:
    source = tuple(
        dataset_urn("postgres", f"source_postgres.recsys.public.{table}")
        for table in RAW_GENERATOR_TABLES
    )
    topics = tuple(
        dataset_urn("kafka", f"recsys-dataflow.cdc.{table}")
        for table in RAW_GENERATOR_TABLES
    )
    return source + topics


def _assertion_urn(dataset: str, kind: str) -> str:
    return f"urn:li:assertion:{uuid.uuid5(ASSERTION_NAMESPACE, f'{dataset}:{kind}')}"


def _old_governed_dataset_urns() -> tuple[str, ...]:
    current_non_analytics = tuple(
        dataset.urn
        for product in catalog_products()
        if product.id != "ANALYTICS"
        for dataset in product.datasets
    )
    return tuple(sorted(set(current_non_analytics + legacy_dataset_urns())))


def legacy_assertion_urns() -> tuple[str, ...]:
    return tuple(
        urn
        for dataset in _old_governed_dataset_urns()
        for urn in (_assertion_urn(dataset, "schema"), _assertion_urn(dataset, "data_quality"))
    )


def _contract_id(dataset: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{dataset}-contract").strip("-").lower()
    return value[:180] or "recsys-data-contract"


def legacy_contract_urns() -> tuple[str, ...]:
    return tuple(f"urn:li:dataContract:{_contract_id(dataset)}" for dataset in _old_governed_dataset_urns())


def _find_named_entities(graph: DataHubGraph, entity_type: str, names: tuple[str, ...]) -> list[str]:
    found: set[str] = set()
    for name in names:
        result = graph.execute_graphql(
            "query search($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) "
            "{ searchResults { entity { urn ... on DataProduct { properties { name } } } } } }",
            {"input": {"types": [entity_type], "query": name, "start": 0, "count": 25}},
        )
        for item in result.get("searchAcrossEntities", {}).get("searchResults", []):
            entity = item.get("entity") or {}
            if (entity.get("properties") or {}).get("name") == name:
                found.add(str(entity["urn"]))
    return sorted(found)


def _record(graph: DataHubGraph, urn: str, entity_type: str, reason: str) -> dict[str, Any]:
    status = graph.get_aspect(urn, StatusClass)
    return {
        "urn": urn,
        "entity_type": entity_type,
        "was_removed": bool(status.removed) if status else False,
        "reason": reason,
    }


def build_manifest(graph: DataHubGraph) -> dict[str, Any]:
    records: dict[str, dict[str, Any]] = {}
    for flow in known_flow_urns():
        if not graph.exists(flow):
            continue
        for job in related_jobs(graph, flow):
            for instance in related_process_instances(graph, job):
                records[instance] = _record(graph, instance, "DATA_PROCESS_INSTANCE", "runtime-lineage-removed")
            records[job] = _record(graph, job, "DATA_JOB", "runtime-lineage-removed")
        records[flow] = _record(graph, flow, "DATA_FLOW", "runtime-lineage-removed")
    static_targets = (
        [(urn, "DATASET", "cdc-catalog-removed") for urn in legacy_dataset_urns()]
        + [(urn, "ASSERTION", "assertion-writeback-removed") for urn in legacy_assertion_urns()]
        + [(urn, "DATA_CONTRACT", "data-contract-writeback-removed") for urn in legacy_contract_urns()]
        + [(f"urn:li:tag:{tag}", "TAG", "legacy-tag-removed") for tag in (
            "CDC_INGESTION", "STREAMING_FEATURES", "DataContract", "NativePipeline"
        )]
    )
    for urn, entity_type, reason in static_targets:
        if graph.exists(urn):
            records[urn] = _record(graph, urn, entity_type, reason)
    for urn in _find_named_entities(graph, "DATA_PRODUCT", ("CDC_INGESTION", "STREAMING_FEATURES")):
        records[urn] = _record(graph, urn, "DATA_PRODUCT", "streaming-product-removed")
    ordered = sorted(records.values(), key=lambda item: (item["entity_type"], item["urn"]))
    return {"version": 1, "records": ordered, "counts": dict(Counter(item["entity_type"] for item in ordered))}


def verify_replacement_catalog(graph: DataHubGraph) -> None:
    products = catalog_products()
    coverage = validate_catalog(products)
    product_urns = _find_named_entities(
        graph, "DATA_PRODUCT", tuple(product.name for product in products)
    )
    if len(product_urns) != coverage["data_products"]:
        raise RuntimeError(
            f"Replacement catalog expected {coverage['data_products']} Data Products, "
            f"found {len(product_urns)}"
        )
    expected_edges = {
        (upstream, dataset.urn)
        for product in products
        for dataset in product.datasets
        for upstream in dataset.upstreams
    }
    observed_edges: set[tuple[str, str]] = set()
    for product in products:
        for dataset in product.datasets:
            if not graph.exists(dataset.urn):
                raise RuntimeError(f"Replacement catalog missing dataset: {dataset.urn}")
            aspect = graph.get_aspect(dataset.urn, UpstreamLineageClass)
            observed_edges.update((str(item.dataset), dataset.urn) for item in (aspect.upstreams if aspect else []))
    if observed_edges != expected_edges:
        raise RuntimeError(
            "Replacement catalog lineage mismatch: "
            f"missing={sorted(expected_edges - observed_edges)}, "
            f"unexpected={sorted(observed_edges - expected_edges)}"
        )


def set_removed(client: DataHubClient, graph: DataHubGraph, records: list[dict[str, Any]], removed: bool) -> None:
    for record in records:
        if removed:
            if not record["was_removed"]:
                client.entities.delete(record["urn"], check_exists=False, hard=False)
        else:
            graph.emit_mcp(MetadataChangeProposalWrapper(
                entityUrn=record["urn"], aspect=StatusClass(removed=bool(record["was_removed"])),
            ))


def apply_manifest(client: DataHubClient, graph: DataHubGraph, manifest: dict[str, Any]) -> None:
    verify_replacement_catalog(graph)
    set_removed(client, graph, manifest["records"], True)


def restore_manifest(client: DataHubClient, graph: DataHubGraph, manifest: dict[str, Any]) -> None:
    set_removed(client, graph, manifest["records"], False)


def _clients() -> tuple[DataHubClient, DataHubGraph]:
    token = (os.getenv("DATAHUB_TOKEN") or os.getenv("DATAHUB_GMS_TOKEN") or "").strip()
    graph = DataHubGraph(DatahubClientConfig(
        server=os.getenv("DATAHUB_GMS_URL", "http://localhost:8088"), token=token or None,
        timeout_sec=180, retry_max_times=5, datahub_component="recsys-catalog-cutover",
    ))
    return DataHubClient(graph=graph), graph


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--restore", action="store_true")
    parser.add_argument("--confirm-cutover", action="store_true")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    client, graph = _clients()
    try:
        if args.restore:
            manifest = json.loads(args.manifest.read_text())
            restore_manifest(client, graph, manifest)
            print(json.dumps({"restored": manifest["counts"]}, indent=2, sort_keys=True))
            return 0
        if not args.apply:
            manifest = build_manifest(graph)
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            print(json.dumps({"dry_run": True, **manifest}, indent=2, sort_keys=True))
            return 0
        if not args.confirm_cutover:
            raise RuntimeError("--apply requires --confirm-cutover")
        # Apply exactly the reviewed dry-run manifest. Never rediscover or
        # rewrite targets during the destructive phase.
        manifest = json.loads(args.manifest.read_text())
        apply_manifest(client, graph, manifest)
        print(json.dumps({"soft_deleted": manifest["counts"]}, indent=2, sort_keys=True))
        return 0
    finally:
        graph.close()


if __name__ == "__main__":
    raise SystemExit(main())
