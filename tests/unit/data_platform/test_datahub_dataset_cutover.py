from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _module():
    path = Path("ops/migrations/datahub-dataset-lineage-cutover/cutover.py")
    spec = importlib.util.spec_from_file_location(
        "datahub_dataset_lineage_cutover", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Graph:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.emitted = []

    def exists(self, urn):
        return urn in self.existing

    def get_aspect(self, _urn, _aspect):
        return None

    def execute_graphql(self, query, variables):
        if "searchAcrossEntities" in query:
            return {"searchAcrossEntities": {"searchResults": []}}
        return {"entity": {"relationships": {"relationships": []}}}

    def emit_mcp(self, proposal):
        self.emitted.append(proposal)

    def close(self):
        pass


def test_manifest_never_targets_kept_static_datasets():
    module = _module()
    legacy = module.legacy_dataset_urns()[0]
    manifest = module.build_manifest(Graph({legacy}))
    targets = {record["urn"] for record in manifest["records"]}
    assert legacy in targets
    kept = {
        dataset.urn
        for product in module.catalog_products()
        for dataset in product.datasets
    }
    assert targets.isdisjoint(kept)


def test_legacy_contract_cleanup_excludes_current_catalog_contracts():
    module = _module()
    current_datasets = {
        dataset.urn
        for product in module.catalog_products()
        for dataset in product.datasets
    }
    assert set(module.legacy_contract_urns()) == {
        f"urn:li:dataContract:{module._contract_id(dataset)}"
        for dataset in module.legacy_dataset_urns()
    }
    assert all(
        dataset not in module.legacy_dataset_urns() for dataset in current_datasets
    )


def test_removed_rag_reconciliation_flow_remains_a_historical_cleanup_target():
    module = _module()
    assert (
        "urn:li:dataFlow:(airflow,recsys_rag_item_reconciliation,PROD)"
        in module.known_flow_urns()
    )


def test_manifest_classifies_flow_job_and_process_instance_without_mutating():
    module = _module()
    flow = module.known_flow_urns()[0]
    job = "urn:li:dataJob:(airflow,recsys_dp1_raw_to_bronze,ingest_stage)"
    process_type = "DATA_PROCESS_" + "INSTANCE"
    process = "urn:li:dataProcess" + "Instance:run-1"

    class RelationshipGraph(Graph):
        def execute_graphql(self, query, variables):
            if "urn" not in variables:
                return {"searchAcrossEntities": {"searchResults": []}}
            urn = variables["urn"]
            related = []
            if urn == flow:
                related = [{"entity": {"urn": job, "type": "DATA_JOB"}}]
            elif urn == job:
                related = [{"entity": {"urn": process, "type": process_type}}]
            return {"entity": {"relationships": {"relationships": related}}}

    graph = RelationshipGraph({flow})
    manifest = module.build_manifest(graph)
    records = {record["urn"]: record for record in manifest["records"]}
    assert records[flow]["entity_type"] == "DATA_FLOW"
    assert records[job]["entity_type"] == "DATA_JOB"
    assert records[process]["entity_type"] == process_type
    assert graph.emitted == []


def test_relationship_query_supplies_required_datahub_types_input():
    module = _module()

    class StrictGraph(Graph):
        def execute_graphql(self, query, variables):
            assert "types: []" in query
            assert variables == {"urn": "urn:li:dataFlow:(airflow,flow,PROD)"}
            return {"entity": {"relationships": {"relationships": []}}}

    assert (
        module.related_jobs(StrictGraph(), "urn:li:dataFlow:(airflow,flow,PROD)") == []
    )


def test_apply_soft_deletes_only_records_not_previously_removed(monkeypatch):
    module = _module()
    deleted = []
    client = type(
        "Client",
        (),
        {
            "entities": type(
                "Entities",
                (),
                {"delete": lambda self, urn, **kwargs: deleted.append(urn)},
            )()
        },
    )()
    verified = []
    monkeypatch.setattr(
        module, "verify_replacement_catalog", lambda graph: verified.append(graph)
    )
    manifest = {
        "records": [
            {"urn": "urn:one", "was_removed": False},
            {"urn": "urn:two", "was_removed": True},
        ]
    }
    module.apply_manifest(client, Graph(), manifest)
    assert len(verified) == 1
    assert deleted == ["urn:one"]


def test_restore_replays_the_original_removed_state():
    module = _module()
    graph = Graph()
    manifest = {
        "records": [
            {"urn": "urn:li:tag:one", "was_removed": False},
            {"urn": "urn:li:tag:two", "was_removed": True},
        ]
    }
    module.restore_manifest(object(), graph, manifest)
    assert [item.aspect.removed for item in graph.emitted] == [False, True]


def test_cli_apply_requires_explicit_confirmation(monkeypatch, tmp_path):
    module = _module()
    graph = Graph()
    monkeypatch.setattr(module, "_clients", lambda: (object(), graph))
    monkeypatch.setattr(
        "sys.argv",
        ["cutover", "--apply", "--manifest", str(tmp_path / "manifest.json")],
    )
    with pytest.raises(RuntimeError, match="--confirm-cutover"):
        module.main()


def test_cli_apply_uses_the_reviewed_manifest_verbatim(monkeypatch, tmp_path):
    module = _module()
    graph = Graph()
    manifest = {
        "version": 1,
        "counts": {"DATA_JOB": 1},
        "records": [
            {
                "urn": "urn:li:dataJob:(airflow,flow,job)",
                "entity_type": "DATA_JOB",
                "was_removed": False,
                "reason": "runtime-lineage-removed",
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    applied = []
    monkeypatch.setattr(module, "_clients", lambda: (object(), graph))
    monkeypatch.setattr(
        module, "apply_manifest", lambda _client, _graph, value: applied.append(value)
    )
    monkeypatch.setattr(
        module,
        "build_manifest",
        lambda _graph: (_ for _ in ()).throw(AssertionError("must not rediscover")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "cutover",
            "--apply",
            "--confirm-cutover",
            "--manifest",
            str(path),
        ],
    )
    assert module.main() == 0
    assert applied == [manifest]
