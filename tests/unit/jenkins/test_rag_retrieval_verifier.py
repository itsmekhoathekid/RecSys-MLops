from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
SPEC = importlib.util.spec_from_file_location(
    "rag_retrieval_verifier", ROOT / "scripts/rag/verify_retrieval.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_golden_config_expands_to_at_least_thirty_unique_vietnamese_queries():
    config = json.loads(
        (ROOT / "configs/data-platform/rag/golden_queries.json").read_text()
    )
    cases = MODULE.build_cases(config)
    assert len(cases) == 30
    assert len({case.query for case in cases}) == 30
    assert all(case.expected_item_ids for case in cases)


def test_verifier_checks_recall_latency_uniqueness_and_constraints(monkeypatch):
    cases = [
        MODULE.GoldenCase("Sony tai nghe", frozenset({1}), "Sony", ("Điện tử",))
    ]

    def response(_url, payload, _timeout):
        item = {
            "item_id": 1,
            "brand": "Sony",
            "category_path": ["Điện tử", "Âm thanh"],
            "in_stock": True,
            "current_price": 20.99,
        }
        return {"items": [item]}, 12.0

    monkeypatch.setattr(MODULE, "_post_json", response)
    report = MODULE.verify(
        base_url="http://rag",
        cases=cases,
        minimum_recall=0.9,
        maximum_p95_ms=750,
        concurrency=10,
        timeout=1,
    )
    assert report["status"] == "passed"
    assert report["hard_constraint_violation_count"] == 0


def test_verifier_fails_gate_for_duplicates_or_constraint_violation(monkeypatch):
    case = MODULE.GoldenCase("q", frozenset({1}), "Sony", ("Điện tử",))

    def response(_url, payload, _timeout):
        bad = {
            "item_id": 2,
            "brand": "Bose",
            "category_path": ["Khác"],
            "in_stock": False,
            "current_price": 2000,
        }
        return {"items": [bad, bad]}, 800.0

    monkeypatch.setattr(MODULE, "_post_json", response)
    report = MODULE.verify(
        base_url="http://rag",
        cases=[case],
        minimum_recall=0.9,
        maximum_p95_ms=750,
        concurrency=1,
        timeout=1,
    )
    assert report["status"] == "failed"
    assert report["duplicate_response_count"] == 1
    assert report["hard_constraint_violation_count"] == 2


def test_recall_gate_scales_with_indexed_catalog_coverage(monkeypatch):
    case = MODULE.GoldenCase(
        "Sony tai nghe", frozenset({1, 2, 3, 4, 5}), "Sony", ("Điện tử",)
    )

    def response(_url, _payload, _timeout):
        items = [
            {
                "item_id": item_id,
                "brand": "Sony",
                "category_path": ["Điện tử", "Âm thanh"],
                "in_stock": True,
                "current_price": 20.99,
            }
            for item_id in (1, 2, 3)
        ]
        return {"items": items}, 12.0

    monkeypatch.setattr(MODULE, "_post_json", response)
    report = MODULE.verify(
        base_url="http://rag",
        cases=[case],
        minimum_recall=0.9,
        maximum_p95_ms=750,
        concurrency=1,
        timeout=1,
        indexed_item_count=96,
        golden_catalog_item_count=160,
    )

    assert report["status"] == "passed"
    assert report["recall_at_10"] == 0.6
    assert report["minimum_recall_at_10"] == 0.9
    assert report["catalog_coverage_ratio"] == 0.6
    assert report["effective_minimum_recall_at_10"] == 0.54
    assert report["indexed_item_count"] == 96
    assert report["golden_catalog_item_count"] == 160


def test_full_catalog_keeps_unscaled_recall_gate(monkeypatch):
    case = MODULE.GoldenCase(
        "Sony tai nghe", frozenset({1, 2, 3, 4, 5}), "Sony", ("Điện tử",)
    )

    def response(_url, _payload, _timeout):
        items = [
            {
                "item_id": item_id,
                "brand": "Sony",
                "category_path": ["Điện tử"],
                "in_stock": True,
                "current_price": 20.99,
            }
            for item_id in (1, 2, 3)
        ]
        return {"items": items}, 12.0

    monkeypatch.setattr(MODULE, "_post_json", response)
    report = MODULE.verify(
        base_url="http://rag",
        cases=[case],
        minimum_recall=0.9,
        maximum_p95_ms=750,
        concurrency=1,
        timeout=1,
        indexed_item_count=160,
        golden_catalog_item_count=160,
    )

    assert report["status"] == "failed"
    assert report["effective_minimum_recall_at_10"] == 0.9


def test_indexed_count_uses_cli_then_jenkins_contract_then_full_catalog(monkeypatch):
    monkeypatch.setenv("RAG_EXPECTED_ITEM_COUNT", "96")
    assert MODULE._resolve_indexed_item_count(80, 160) == 80
    assert MODULE._resolve_indexed_item_count(0, 160) == 96
    monkeypatch.delenv("RAG_EXPECTED_ITEM_COUNT")
    assert MODULE._resolve_indexed_item_count(0, 160) == 160
