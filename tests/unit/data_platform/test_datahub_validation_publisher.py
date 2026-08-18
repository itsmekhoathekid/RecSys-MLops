from __future__ import annotations

from types import SimpleNamespace

import pytest

from metadata.datahub_client import DataHubCatalogClient
from metadata.governance_catalog import assertion_urn, dp1_product
from validate.report_io import (
    DatasetValidationResult,
    ValidationReport,
    read_validation_report,
    validation_report,
    write_validation_report,
)


def test_validation_report_round_trip(tmp_path):
    uri = str(tmp_path / "report.json")
    report = validation_report(
        "DP1",
        "scheduled__2026-08-18",
        {
            "bronze.users": {
                "status": "SUCCESS",
                "checks": [{"name": "row_count", "status": "SUCCESS"}],
            }
        },
        report_uri=uri,
    )
    write_validation_report(report, uri)
    restored = read_validation_report(uri)
    assert restored == report
    assert restored.datasets[0].dataset_key == "bronze.users"


def test_publish_maps_results_and_synthesizes_missing_dataset_error():
    calls = []
    graph = SimpleNamespace(
        report_assertion_result=lambda **kwargs: calls.append(kwargs)
    )
    client = DataHubCatalogClient.__new__(DataHubCatalogClient)
    client._graph = graph
    report = ValidationReport(
        schema_version=1,
        product_id="DP1",
        run_id="run-1",
        generated_at="2026-08-18T00:00:00+00:00",
        datasets=(
            DatasetValidationResult(
                dataset_key="bronze.users",
                status="SUCCESS",
                checks=({"name": "row_count", "status": "SUCCESS"},),
            ),
        ),
        report_uri="s3://bucket/report.json",
    )
    summary = client.publish_validation_reports(
        "DP1", (report,), ("bronze.users", "bronze.products")
    )
    assert (summary.success, summary.failure, summary.error) == (1, 0, 1)
    datasets = {
        dataset.contract.validation_key: dataset for dataset in dp1_product().datasets
    }
    assert calls[0]["urn"] == assertion_urn(datasets["bronze.users"].urn)
    assert calls[0]["type"] == "SUCCESS"
    assert calls[1]["type"] == "ERROR"
    assert calls[1]["error_type"] == "UNKNOWN_ERROR"


def test_publish_rejects_duplicate_and_unknown_validation_keys():
    client = DataHubCatalogClient.__new__(DataHubCatalogClient)
    client._graph = SimpleNamespace(report_assertion_result=lambda **_kwargs: True)
    result = DatasetValidationResult("bronze.users", "SUCCESS")
    report = ValidationReport(1, "DP1", "run", "now", (result, result))
    with pytest.raises(ValueError, match="Duplicate"):
        client.publish_validation_reports("DP1", (report,), ("bronze.users",))
    with pytest.raises(ValueError, match="Unknown expected"):
        client.publish_validation_reports("DP1", (), ("bronze.missing",))


def test_contract_upsert_reuses_existing_urn_and_replaces_assertion(monkeypatch):
    calls = []

    class Graph:
        def execute_graphql(self, query, variables=None):
            calls.append((query, variables))
            return {"upsertDataContract": {"urn": "urn:li:dataContract:existing"}}

        def set_soft_delete_status(self, urn, delete):
            calls.append(("status", {"urn": urn, "delete": delete}))

    client = DataHubCatalogClient.__new__(DataHubCatalogClient)
    client._graph = Graph()
    monkeypatch.setattr(
        client, "_dataset_contract", lambda _urn: "urn:li:dataContract:existing"
    )
    dataset = dp1_product().datasets[0]
    resolved = client._upsert_data_contract(dataset, assertion_urn(dataset.urn))
    assert resolved == "urn:li:dataContract:existing"
    mutation = calls[0][0]
    assert dataset.urn in mutation
    assert assertion_urn(dataset.urn) in mutation
    assert "freshness: []" in mutation and "schema: []" in mutation
    assert calls[-1] == (
        "status",
        {"urn": "urn:li:dataContract:existing", "delete": False},
    )


def test_removed_legacy_contract_is_discovered_for_reactivation():
    dataset = dp1_product().datasets[0]
    legacy_id = (
        __import__("re")
        .sub(r"[^A-Za-z0-9_.-]+", "-", f"{dataset.urn}-contract")
        .strip("-")
        .lower()[:180]
    )
    legacy_urn = f"urn:li:dataContract:{legacy_id}"

    class Graph:
        def execute_graphql(self, _query, _variables):
            return {"dataset": {"contract": None}}

        def exists(self, urn):
            return urn == legacy_urn

    client = DataHubCatalogClient.__new__(DataHubCatalogClient)
    client._graph = Graph()
    assert client._dataset_contract(dataset.urn) == legacy_urn
