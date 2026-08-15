#!/usr/bin/env python3
"""Preview, apply, or restore the DataHub SDK lineage identity cutover."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.metadata.schema_classes import (
    DataJobInputOutputClass,
    DataProcessInstanceRunEventClass,
    DataProcessRunStatusClass,
    StatusClass,
)

from metadata.governance_catalog import flow_urn, job_urn, pipeline_flow_urn
from metadata.ingest_datahub_governance import (
    GovernanceEmitter,
    cdc_ingestion,
    dp1,
    dp2,
    dp3,
    streaming_features,
)


DEFAULT_MANIFEST = Path("datahub-sdk-lineage-cutover-manifest.json")


def products():
    return (dp1(), dp2(), dp3(), cdc_ingestion(), streaming_features())


def related_process_instances(graph, template_urn: str) -> list[str]:
    data = graph.execute_graphql(
        """
        query processInstances($urn: String!) {
          entity(urn: $urn) {
            relationships(input: {
              types: ["IsInstanceOf"]
              direction: INCOMING
              start: 0
              count: 1000
            }) {
              relationships { entity { urn type } }
            }
          }
        }
        """,
        {"urn": template_urn},
    )
    relationships = ((data.get("entity") or {}).get("relationships") or {}).get(
        "relationships", []
    )
    return sorted(
        {
            item["entity"]["urn"]
            for item in relationships
            if (item.get("entity") or {}).get("type") == "DATA_PROCESS_INSTANCE"
        }
    )


def successful_process_instances(graph, template_urn: str) -> list[str]:
    successful: list[str] = []
    for urn in related_process_instances(graph, template_urn):
        event = graph.get_aspect(urn, DataProcessInstanceRunEventClass)
        if (
            event is not None
            and event.status == DataProcessRunStatusClass.COMPLETE
            and event.result is not None
            and event.result.type == "SUCCESS"
        ):
            successful.append(urn)
    return successful


def build_manifest(emitter: GovernanceEmitter) -> dict[str, Any]:
    legacy_jobs: list[str] = []
    new_jobs: list[str] = []
    legacy_flows: list[str] = []
    legacy_instances: set[str] = set()

    for product in products():
        old_flow = flow_urn(product.flow_id)
        new_flow = pipeline_flow_urn(product.id)
        if old_flow != new_flow and emitter.graph.exists(old_flow):
            legacy_flows.append(old_flow)
        for job in product.jobs:
            old_job = job_urn(old_flow, f"{product.flow_id}.{job.id}")
            new_job = job_urn(new_flow, job.id)
            new_jobs.append(new_job)
            if emitter.graph.exists(old_job):
                legacy_jobs.append(old_job)
                legacy_instances.update(
                    related_process_instances(emitter.graph, old_job)
                )

    return {
        "legacy_process_instances": sorted(legacy_instances),
        "legacy_data_jobs": sorted(set(legacy_jobs)),
        "legacy_data_flows": sorted(set(legacy_flows)),
        "required_new_data_jobs": sorted(set(new_jobs)),
    }


def validate_cutover(emitter: GovernanceEmitter, manifest: dict[str, Any]) -> None:
    errors: list[str] = []
    for urn in manifest["required_new_data_jobs"]:
        if not emitter.graph.exists(urn):
            errors.append(f"missing new DataJob: {urn}")
            continue
        if emitter.graph.get_aspect(urn, DataJobInputOutputClass) is None:
            errors.append(f"missing new DataJob lineage: {urn}")
        if not successful_process_instances(emitter.graph, urn):
            errors.append(f"missing successful cutover run evidence: {urn}")
    if errors:
        raise RuntimeError("Cutover validation failed: " + " | ".join(errors))


def set_removed(emitter: GovernanceEmitter, urns: list[str], removed: bool) -> None:
    for urn in urns:
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=StatusClass(removed=removed),
            )
        )


def ordered_legacy_targets(manifest: dict[str, Any]) -> list[str]:
    return [
        *manifest["legacy_process_instances"],
        *manifest["legacy_data_jobs"],
        *manifest["legacy_data_flows"],
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--restore", action="store_true")
    parser.add_argument("--confirm-cutover", action="store_true")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    emitter = GovernanceEmitter(os.getenv("DATAHUB_GMS_URL", "http://localhost:8088"))
    try:
        if args.restore:
            manifest = json.loads(args.manifest.read_text())
            set_removed(emitter, ordered_legacy_targets(manifest), False)
            print(json.dumps({"restored": ordered_legacy_targets(manifest)}, indent=2))
            return 0

        manifest = build_manifest(emitter)
        args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        if not args.apply:
            print(json.dumps({"dry_run": True, **manifest}, indent=2, sort_keys=True))
            return 0
        if not args.confirm_cutover:
            raise RuntimeError("--apply requires --confirm-cutover")
        validate_cutover(emitter, manifest)
        targets = ordered_legacy_targets(manifest)
        set_removed(emitter, targets, True)
        print(json.dumps({"soft_deleted": targets}, indent=2))
        return 0
    finally:
        emitter.close()


if __name__ == "__main__":
    raise SystemExit(main())
