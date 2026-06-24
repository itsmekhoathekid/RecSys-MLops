from __future__ import annotations

from types import SimpleNamespace
import sys

import pandas as pd

from dataset_versioning import _spark_safe_records, sample_id_for, row_hash_for, to_versioned_samples
from prepare_bst_training_data import (
    DEFAULT_FEATURE_SERVICE_NAME,
    FEAST_FEATURE_REFS,
    build_bst_training_table_from_feast,
    prepare_bst_jsonl_splits,
)


def _write_labels(tmp_path, rows: list[dict]) -> str:
    target = tmp_path / "labels"
    target.mkdir()
    pd.DataFrame(rows).to_parquet(target / "part-00000.parquet", index=False)
    return str(target)


def _install_fake_feast(monkeypatch, historical: pd.DataFrame, captured: dict, feature_service: object | None = None) -> None:
    class FakeRetrieval:
        def to_df(self):
            return historical.copy()

    class FakeFeatureStore:
        def __init__(self, repo_path: str):
            captured["repo_path"] = repo_path

        def get_feature_service(self, name):
            captured["feature_service_name"] = name
            if feature_service is None:
                raise KeyError(name)
            return feature_service

        def get_historical_features(self, entity_df, features, full_feature_names):
            captured["entity_df"] = entity_df.copy()
            captured["features"] = features
            captured["full_feature_names"] = full_feature_names
            return FakeRetrieval()

    monkeypatch.setitem(sys.modules, "feast", SimpleNamespace(FeatureStore=FakeFeatureStore))
    monkeypatch.setattr("prepare_bst_training_data._apply_feast_repo", lambda repo_path: None)


def test_build_bst_training_table_from_feast_maps_historical_features(monkeypatch, tmp_path):
    labels_path = _write_labels(
        tmp_path,
        [
            {
                "impression_id": "imp-1",
                "request_id": "req-1",
                "user_id": 7,
                "candidate_product_id": 11,
                "prediction_timestamp": pd.Timestamp("2026-01-01T00:10:00Z"),
                "label": 1,
            }
        ],
    )
    historical = pd.DataFrame(
        [
            {
                "row_id": 0,
                "user_sequence_features__hist_item_ids": [9, 10],
                "user_sequence_features__hist_event_type_ids": [1, 2],
                "user_sequence_features__hist_category_ids": [3, 4],
                "user_sequence_features__hist_brand_ids": [5, 6],
                "user_sequence_features__hist_price_bucket_ids": [7, 8],
                "user_sequence_features__hist_event_timestamps": [
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:05:00+00:00",
                ],
                "user_aggregate_features__views_30m": 3,
                "user_aggregate_features__carts_30m": 2,
                "user_aggregate_features__purchases_24h": 1,
                "item_features__category_id": 22,
                "item_features__brand_id": 33,
                "item_features__price_bucket": 44,
            }
        ]
    )
    captured: dict = {}
    feature_service = object()
    _install_fake_feast(monkeypatch, historical, captured, feature_service=feature_service)

    training = build_bst_training_table_from_feast(
        labels_path,
        feast_repo_path="/opt/recsys/apps/data-platform/feature-store/feature_repo",
        max_history_len=1,
        feast_offline_root="/workspace/recsys/data_platform/output/feature_store/offline",
    )

    assert captured["feature_service_name"] == DEFAULT_FEATURE_SERVICE_NAME
    assert captured["features"] is feature_service
    assert captured["full_feature_names"] is True
    assert captured["entity_df"][["user_id", "product_id"]].to_dict("records") == [
        {"user_id": 7, "product_id": 11}
    ]
    row = training.iloc[0].to_dict()
    assert row["hist_item_id"] == [10]
    assert row["hist_event_type"] == [2]
    assert row["hist_time"] == [2]
    assert row["target_item_id"] == 11
    assert row["target_category"] == 22
    assert row["target_brand"] == 33
    assert row["target_price_bucket"] == 44
    assert row["label"] == 1


def test_build_bst_training_table_can_fallback_to_feature_refs(monkeypatch, tmp_path):
    labels_path = _write_labels(
        tmp_path,
        [
            {
                "impression_id": "imp-1",
                "request_id": "req-1",
                "user_id": 7,
                "candidate_product_id": 11,
                "prediction_timestamp": pd.Timestamp("2026-01-01T00:10:00Z"),
                "label": 1,
            }
        ],
    )
    historical = pd.DataFrame(
        [
            {
                "row_id": 0,
                "user_sequence_features__hist_item_ids": [9],
                "user_sequence_features__hist_event_type_ids": [1],
                "user_sequence_features__hist_category_ids": [3],
                "user_sequence_features__hist_brand_ids": [5],
                "user_sequence_features__hist_price_bucket_ids": [7],
                "user_sequence_features__hist_event_timestamps": ["2026-01-01T00:00:00+00:00"],
                "item_features__category_id": 22,
                "item_features__brand_id": 33,
                "item_features__price_bucket": 44,
            }
        ]
    )
    captured: dict = {}
    _install_fake_feast(monkeypatch, historical, captured)

    build_bst_training_table_from_feast(labels_path, feast_repo_path="/repo", max_history_len=1)

    assert captured["features"] == FEAST_FEATURE_REFS


def test_prepare_splits_records_feast_source(monkeypatch, tmp_path):
    labels_path = _write_labels(
        tmp_path,
        [
            {
                "impression_id": f"imp-{index}",
                "request_id": f"req-{index}",
                "user_id": 7,
                "candidate_product_id": 11 + index,
                "prediction_timestamp": pd.Timestamp("2026-01-01T00:10:00Z") + pd.Timedelta(minutes=index),
                "label": index % 2,
            }
            for index in range(5)
        ],
    )
    historical = pd.DataFrame(
        [
            {
                "row_id": index,
                "user_sequence_features__hist_item_ids": [9, 10],
                "user_sequence_features__hist_event_type_ids": [1, 2],
                "user_sequence_features__hist_category_ids": [3, 4],
                "user_sequence_features__hist_brand_ids": [5, 6],
                "user_sequence_features__hist_price_bucket_ids": [7, 8],
                "user_sequence_features__hist_event_timestamps": [
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:05:00+00:00",
                ],
                "item_features__category_id": 22,
                "item_features__brand_id": 33,
                "item_features__price_bucket": 44,
            }
            for index in range(5)
        ]
    )
    _install_fake_feast(monkeypatch, historical, {})

    metadata = prepare_bst_jsonl_splits(
        entity_input_path=labels_path,
        output_dir=tmp_path / "splits",
        train_ratio=0.6,
        val_ratio=0.2,
        max_history_len=2,
        feast_repo_path="/repo",
        feast_offline_root="/features",
    )

    assert metadata["feature_source"] == "feast"
    assert metadata["feature_service_name"] == DEFAULT_FEATURE_SERVICE_NAME
    assert metadata["entity_input_path"] == labels_path
    assert metadata["feast_repo_path"] == "/repo"
    assert metadata["feast_offline_root"] == "/features"
    assert metadata["train_rows"] == 3
    assert metadata["val_rows"] == 1
    assert metadata["test_rows"] == 1
    assert metadata["iceberg"]["enabled"] is False
    assert (tmp_path / "splits" / "dataset_version_meta.json").exists()
    assert (tmp_path / "splits" / "train.jsonl").exists()


def test_versioned_samples_use_stable_sample_id_and_split_routes():
    row = {
        "impression_id": "imp-1",
        "request_id": "req-1",
        "user_id": 7,
        "target_item_id": 11,
        "event_time": 1767226200,
        "hist_item_id": [10],
        "hist_event_type": [2],
        "hist_category": [3],
        "hist_brand": [4],
        "hist_price_bucket": [5],
        "hist_time": [1],
        "target_category": 22,
        "target_brand": 33,
        "target_price_bucket": 44,
        "label": 1,
    }

    assert sample_id_for(row) == sample_id_for(dict(row))
    assert row_hash_for(row) == row_hash_for(dict(row))
    samples = to_versioned_samples(
        {"train": [row], "val": [], "test": [dict(row, request_id="req-2")]},
        dataset_run_id="run-1",
        feature_service_version="bst_ranking_v1",
        processing_code="abc123",
    )

    assert samples["sample_id"].nunique() == 2
    assert samples.groupby("split").size().to_dict() == {"test": 1, "train": 1}
    assert samples["feature_service_version"].unique().tolist() == ["bst_ranking_v1"]


def test_spark_safe_records_convert_timezone_aware_timestamps():
    samples = pd.DataFrame(
        [
            {
                "sample_id": "s1",
                "entity_id": "7",
                "user_id": 7,
                "target_item_id": 11,
                "event_timestamp": pd.Timestamp("2026-01-01T00:10:00Z"),
                "split": "train",
                "label": 1,
                "hist_item_id": [10],
                "hist_event_type": [2],
                "hist_category": [3],
                "hist_brand": [4],
                "hist_price_bucket": [5],
                "hist_time": [1],
                "target_category": 22,
                "target_brand": 33,
                "target_price_bucket": 44,
                "event_time": 1767226200,
                "features_json": "{}",
                "feature_service_version": "bst_ranking_v1",
                "processing_code_version": "abc123",
                "row_hash": "hash",
                "dataset_run_id": "run-1",
                "created_at": pd.Timestamp("2026-01-01T00:11:00Z"),
                "updated_at": pd.Timestamp("2026-01-01T00:12:00Z"),
            }
        ]
    )

    record = _spark_safe_records(samples)[0]

    assert record["event_timestamp"].tzinfo is None
    assert record["created_at"].tzinfo is None
    assert record["updated_at"].tzinfo is None
