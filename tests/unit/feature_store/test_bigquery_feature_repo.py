from __future__ import annotations

import importlib
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]
FEATURE_REPO = ROOT / "apps/data-platform/feature-store/feature_repo"


def test_feast_offline_store_uses_postgres() -> None:
    config = yaml.safe_load((FEATURE_REPO / "feature_store.yaml").read_text())

    assert config["offline_store"]["type"] == "postgres"
    assert config["offline_store"]["host"] == "feature-postgres.recsys-dataflow.svc.cluster.local"
    assert config["offline_store"]["port"] == 5432
    assert config["offline_store"]["db_schema"] == "feature_store"
    assert config["offline_store"]["sslmode"] == "disable"


def test_feature_views_use_postgres_sources(monkeypatch) -> None:
    monkeypatch.setenv("FEAST_POSTGRES_SCHEMA", "unit_schema")
    monkeypatch.syspath_prepend(str(FEATURE_REPO))
    sys.modules.pop("features", None)

    features = importlib.import_module("features")

    assert type(features.user_sequence_source).__name__ == "PostgreSQLSource"
    assert features.user_sequence_source._postgres_options._table == "unit_schema.user_sequence_features"
    assert features.user_aggregate_source._postgres_options._table == "unit_schema.user_aggregate_features"
    assert features.item_features_source._postgres_options._table == "unit_schema.item_features"


def test_feast_objects_do_not_tag_lakehouse_as_feature_store(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(FEATURE_REPO))
    sys.modules.pop("features", None)

    features = importlib.import_module("features")

    feast_objects = [
        features.user_sequence_features,
        features.user_aggregate_features,
        features.item_features,
        features.bst_ranking_v1,
    ]
    for feast_object in feast_objects:
        assert "lakehouse_source" not in feast_object.tags
        assert feast_object.tags["offline_store"] == "postgresql"
