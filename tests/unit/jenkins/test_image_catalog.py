from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.python.image_catalog import (
    dependency_build_args,
    dependency_order,
    load_catalog,
)


def _catalog_payload() -> dict:
    return json.loads((ROOT / "images/catalog.json").read_text(encoding="utf-8"))


def test_catalog_contains_exactly_fifteen_images_and_one_spark() -> None:
    images = load_catalog()

    assert len(images) == 15
    assert {name for name in images if name.endswith("-spark")} == {"recsys-spark"}
    assert all(spec["context"] == "." for spec in images.values())
    assert all((ROOT / spec["dockerfile"]).is_file() for spec in images.values())


def test_internal_dependencies_are_topologically_ordered() -> None:
    assert dependency_order("recsys-data-ingestion") == [
        "recsys-base-python",
        "recsys-data-ingestion",
    ]
    assert dependency_build_args("recsys-data-ingestion", "abc123") == [
        "RECSYS_BASE_IMAGE=recsys-base-python:abc123"
    ]


def test_catalog_rejects_legacy_spark_images(tmp_path: Path) -> None:
    payload = _catalog_payload()
    payload["images"]["recsys-mlops-spark"] = payload["images"].pop(
        "recsys-analytics-dbt"
    )
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden legacy images"):
        load_catalog(catalog_path)


def test_every_catalog_image_is_reachable_from_a_component() -> None:
    # load_catalog performs closure, unknown dependency, and cycle validation.
    assert set(load_catalog()) == set(_catalog_payload()["images"])
