#!/usr/bin/env python3
"""Verify a promoted RAG API with deterministic Vietnamese golden queries.

The script expands the repository's cyclic demo catalog into at least thirty
brand/category relevance judgments, measures Recall@10 and warm request p95 at
concurrency ten, then repeats filtered requests to prove hard constraints have
zero violations. It writes a release-evidence JSON report and exits non-zero on
any failed gate so Jenkins can atomically restore the previous index pointer.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GoldenCase:
    """One query, expected item set, and scalar constraint expectations."""

    query: str
    expected_item_ids: frozenset[int]
    brand: str
    category_path: tuple[str, ...]


def build_cases(config: dict[str, Any]) -> list[GoldenCase]:
    """Expand deterministic brand/category cycles into relevance judgments."""

    start = int(config["catalog_start_item_id"])
    count = int(config["item_count"])
    brands = config["brands"]
    categories = config["categories"]
    cases: list[GoldenCase] = []
    for category in categories:
        for brand in brands:
            expected = frozenset(
                start + offset
                for offset in range(count)
                if offset % len(brands) == int(brand["offset"])
                and offset % len(categories) == int(category["offset"])
            )
            if expected:
                cases.append(
                    GoldenCase(
                        query=f'{brand["name"]} {category["query"]}',
                        expected_item_ids=expected,
                        brand=str(brand["name"]),
                        category_path=tuple(category["path"]),
                    )
                )
    minimum = int(config.get("minimum_queries", 30))
    if len(cases) < minimum:
        raise ValueError(f"Golden catalog produced {len(cases)} queries; need {minimum}")
    return cases[:minimum]


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], float]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read())
    return result, (time.perf_counter() - started) * 1000.0


def _percentile(values: list[float], percentile: float) -> float:
    """Return nearest-rank percentile for a non-empty latency sample."""

    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _resolve_indexed_item_count(cli_value: int, golden_catalog_item_count: int) -> int:
    """Resolve explicit CLI input, then Jenkins' promotion contract, then full size."""

    return (
        cli_value
        or int(os.getenv("RAG_EXPECTED_ITEM_COUNT", "0"))
        or golden_catalog_item_count
    )


def verify(
    *,
    base_url: str,
    cases: list[GoldenCase],
    minimum_recall: float,
    maximum_p95_ms: float,
    concurrency: int,
    timeout: float,
    indexed_item_count: int | None = None,
    golden_catalog_item_count: int | None = None,
) -> dict[str, Any]:
    """Run recall, latency, uniqueness, and hard-constraint release gates."""

    if (indexed_item_count is None) != (golden_catalog_item_count is None):
        raise ValueError(
            "indexed_item_count and golden_catalog_item_count must be provided together"
        )
    coverage_ratio = 1.0
    if indexed_item_count is not None and golden_catalog_item_count is not None:
        if indexed_item_count <= 0 or golden_catalog_item_count <= 0:
            raise ValueError("catalog item counts must be positive")
        coverage_ratio = min(indexed_item_count / golden_catalog_item_count, 1.0)
    effective_minimum_recall = minimum_recall * coverage_ratio

    endpoint = f"{base_url.rstrip('/')}/v1/rag/retrieve"

    def semantic(case: GoldenCase) -> tuple[GoldenCase, dict[str, Any], float]:
        response, latency = _post_json(
            endpoint, {"query": case.query, "top_k_items": 10}, timeout
        )
        return case, response, latency

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        semantic_results = list(executor.map(semantic, cases))

    recalls: list[float] = []
    latencies: list[float] = []
    duplicate_responses = 0
    query_results: list[dict[str, Any]] = []
    for case, response, latency in semantic_results:
        item_ids = [int(item["item_id"]) for item in response.get("items", [])]
        duplicate_responses += int(len(item_ids) != len(set(item_ids)))
        recall = len(set(item_ids) & case.expected_item_ids) / len(case.expected_item_ids)
        recalls.append(recall)
        latencies.append(latency)
        query_results.append(
            {"query": case.query, "recall_at_10": recall, "latency_ms": latency}
        )

    def constrained(case: GoldenCase) -> tuple[GoldenCase, dict[str, Any]]:
        response, _ = _post_json(
            endpoint,
            {
                "query": case.query,
                "top_k_items": 10,
                "filters": {
                    "brands": [case.brand],
                    "category_prefix": list(case.category_path),
                    "min_current_price": 0,
                    "max_current_price": 1000,
                    "in_stock": True,
                },
            },
            timeout,
        )
        return case, response

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        constrained_results = list(executor.map(constrained, cases))
    violations = 0
    constrained_items = 0
    for case, response in constrained_results:
        for item in response.get("items", []):
            constrained_items += 1
            violations += int(
                item["brand"].casefold() != case.brand.casefold()
                or tuple(item["category_path"])[: len(case.category_path)]
                != case.category_path
                or not item["in_stock"]
                or not 0 <= float(item["current_price"]) <= 1000
            )

    mean_recall = sum(recalls) / len(recalls)
    p95_ms = _percentile(latencies, 0.95)
    passed = (
        mean_recall >= effective_minimum_recall
        and p95_ms <= maximum_p95_ms
        and duplicate_responses == 0
        and violations == 0
        and constrained_items > 0
    )
    return {
        "status": "passed" if passed else "failed",
        "query_count": len(cases),
        "recall_at_10": mean_recall,
        "minimum_recall_at_10": minimum_recall,
        "effective_minimum_recall_at_10": effective_minimum_recall,
        "catalog_coverage_ratio": coverage_ratio,
        "indexed_item_count": indexed_item_count,
        "golden_catalog_item_count": golden_catalog_item_count,
        "warm_api_p95_ms": p95_ms,
        "maximum_warm_api_p95_ms": maximum_p95_ms,
        "concurrency": concurrency,
        "duplicate_response_count": duplicate_responses,
        "hard_constraint_items_checked": constrained_items,
        "hard_constraint_violation_count": violations,
        "queries": query_results,
    }


def main() -> int:
    """Parse release-gate options, persist evidence, and return shell status."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--golden", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--minimum-recall", type=float, default=0.90)
    parser.add_argument(
        "--indexed-item-count",
        type=int,
        default=0,
        help=(
            "Indexed corpus size used to scale the full-catalog recall threshold. "
            "Zero assumes the complete golden catalog."
        ),
    )
    parser.add_argument("--maximum-p95-ms", type=float, default=750.0)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    config = json.loads(Path(args.golden).read_text(encoding="utf-8"))
    golden_catalog_item_count = int(config["item_count"])
    indexed_item_count = _resolve_indexed_item_count(
        args.indexed_item_count, golden_catalog_item_count
    )
    report = verify(
        base_url=args.base_url,
        cases=build_cases(config),
        minimum_recall=args.minimum_recall,
        maximum_p95_ms=args.maximum_p95_ms,
        concurrency=args.concurrency,
        timeout=args.timeout,
        indexed_item_count=indexed_item_count,
        golden_catalog_item_count=golden_catalog_item_count,
    )
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
