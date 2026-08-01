from __future__ import annotations

import json
from types import SimpleNamespace
import sys

import pandas as pd
import pytest

from lineage.dataset_versioning import (
    HudiConfig,
    _build_hudi_delta,
    _hudi_options,
    _sample_schema,
    _spark_safe_records,
    to_versioned_samples,
)
from cli.prepare_bst_training_data import (
    DEFAULT_FEATURE_SERVICE_NAME,
    DEFAULT_OFFLINE_FEATURE_TABLE,
    FEAST_FEATURE_REFS,
    MODEL_COLUMNS,
    SplitService,
    TrainingDataService,
    _canonical_entity_frame,
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
    monkeypatch.setattr("cli.prepare_bst_training_data._apply_feast_repo", lambda repo_path: None)


def test_canonical_entity_frame_resets_non_contiguous_index():
    labels = pd.DataFrame(
        [
            {
                "impression_id": "imp-old",
                "request_id": "req-old",
                "user_id": 1,
                "candidate_product_id": 10,
                "prediction_timestamp": pd.Timestamp("2026-01-01T00:00:00Z"),
                "label": 0,
            },
            {
                "impression_id": "imp-new",
                "request_id": "req-new",
                "user_id": 2,
                "candidate_product_id": 20,
                "prediction_timestamp": pd.Timestamp("2026-01-02T00:00:00Z"),
                "label": 1,
            },
        ],
        index=[101, 205],
    )

    entity = _canonical_entity_frame(labels.sort_values("prediction_timestamp").head(2))

    assert entity["event_timestamp"].isna().sum() == 0
    assert entity[["row_id", "user_id", "product_id", "label"]].to_dict("records") == [
        {"row_id": 0, "user_id": 1, "product_id": 10, "label": 0},
        {"row_id": 1, "user_id": 2, "product_id": 20, "label": 1},
    ]


def test_training_data_service_validates_canonical_schema():
    service = TrainingDataService()
    service.validate_schema(pd.DataFrame([{column: 0 for column in MODEL_COLUMNS}]))

    with pytest.raises(ValueError, match="missing columns"):
        service.validate_schema(pd.DataFrame([{"user_id": 1}]))


def test_hudi_dataset_upsert_reconciles_ranking_group_schema():
    options = _hudi_options(
        "bst_samples_native_v2",
        HudiConfig(),
        dataset_run_id="run-1",
        processing_code="abc123",
        feature_service_version="bst_ranking_v1",
    )

    assert options["hoodie.datasource.write.operation"] == "upsert"
    assert options["hoodie.datasource.write.recordkey.field"] == "impression_id,target_item_id"
    assert options["hoodie.index.type"] == "GLOBAL_BLOOM"
    assert options["hoodie.bloom.index.update.partition.path"] == "true"
    assert options["hoodie.datasource.write.reconcile.schema"] == "true"
    assert options["recsys_dataset_run_id"] == "run-1"
    assert options["hoodie.cleaner.policy.failed.writes"] == "LAZY"


def test_hudi_config_reads_native_table_retention_and_zookeeper_env(monkeypatch):
    monkeypatch.setenv("HUDI_DATASET_TABLE", "ml.custom_native")
    monkeypatch.setenv("HUDI_CLEAN_HOURS_RETAINED", "48")
    monkeypatch.setenv("HUDI_ZK_URL", "zk.internal")
    monkeypatch.setenv("HUDI_ZK_PORT", "22181")
    monkeypatch.setenv("HUDI_ZK_BASE_PATH", "/locks/custom")
    monkeypatch.setenv("HUDI_ZK_LOCK_KEY", "custom")

    config = HudiConfig.from_env(warehouse="file:///tmp/hudi-warehouse")

    assert config.dataset_ident == "recsys_features.ml.custom_native"
    assert config.table_path == (
        "file:///tmp/hudi-warehouse/recsys_features/ml/custom_native"
    )
    assert config.clean_hours_retained == 48
    assert config.zookeeper_url == "zk.internal"
    assert config.zookeeper_port == 22181
    assert config.zookeeper_base_path == "/locks/custom"
    assert config.zookeeper_lock_key == "custom"


@pytest.fixture(scope="module")
def local_spark():
    from pyspark.sql import SparkSession

    spark = (
        SparkSession.builder.master("local[1]")
        .appName("recsys-hudi-change-detection-test")
        .config("spark.ui.enabled", "false")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.pyspark.python", sys.executable)
        .config("spark.pyspark.driver.python", sys.executable)
        .getOrCreate()
    )
    yield spark
    spark.stop()


def _versioned_row(impression_id: str, target_item_id: int, **overrides):
    return {
        "impression_id": impression_id,
        "request_id": f"req-{impression_id}",
        "user_id": 7,
        "target_item_id": target_item_id,
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
        **overrides,
    }


def _spark_samples(spark, splits):
    frame = to_versioned_samples(splits)
    return spark.createDataFrame(_spark_safe_records(frame), schema=_sample_schema())


def test_hudi_delta_emits_tombstone_only_for_missing_key(monkeypatch, local_spark):
    existing = _spark_samples(
        local_spark,
        {
            "train": [
                _versioned_row("moved", 11),
                _versioned_row("deleted", 12),
            ],
            "val": [],
            "test": [],
        },
    )
    incoming = _spark_samples(
        local_spark,
        {
            "train": [_versioned_row("new", 13)],
            "val": [_versioned_row("moved", 11)],
            "test": [],
        },
    )
    monkeypatch.setattr("lineage.dataset_versioning._hudi_table_exists", lambda spark, path: True)
    monkeypatch.setattr("lineage.dataset_versioning._read_hudi_table", lambda spark, path: existing)

    delta, compared_with_snapshot, delete_rows = _build_hudi_delta(
        local_spark,
        incoming,
        "ignored",
        pd.Timestamp("2026-01-02T00:00:00Z").to_pydatetime(),
    )
    rows = {
        (row.impression_id, row.target_item_id, row.split, row._hoodie_is_deleted)
        for row in delta.collect()
    }

    assert compared_with_snapshot is True
    assert delete_rows == 1
    assert rows == {
        ("new", 13, "train", False),
        ("moved", 11, "val", False),
        ("deleted", 12, "train", True),
    }


def test_hudi_change_detection_writes_everything_on_initial_load(monkeypatch, local_spark):
    incoming = _spark_samples(
        local_spark,
        {"train": [_versioned_row("new", 11)], "val": [], "test": []},
    )

    monkeypatch.setattr("lineage.dataset_versioning._hudi_table_exists", lambda spark, path: False)

    changes, compared_with_snapshot, delete_rows = _build_hudi_delta(
        local_spark,
        incoming,
        "missing",
        pd.Timestamp("2026-01-02T00:00:00Z").to_pydatetime(),
    )

    assert compared_with_snapshot is False
    assert delete_rows == 0
    assert changes.collect() == incoming.collect()


def test_hudi_delta_does_not_treat_existing_table_read_failure_as_initial_load(
    monkeypatch,
    local_spark,
):
    incoming = _spark_samples(
        local_spark,
        {"train": [_versioned_row("new", 11)], "val": [], "test": []},
    )
    monkeypatch.setattr(
        "lineage.dataset_versioning._hudi_table_exists",
        lambda spark, path: True,
    )

    def failed_read(*_args, **_kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr("lineage.dataset_versioning._read_hudi_table", failed_read)

    with pytest.raises(RuntimeError, match="storage unavailable"):
        _build_hudi_delta(
            local_spark,
            incoming,
            "existing",
            pd.Timestamp("2026-01-02T00:00:00Z").to_pydatetime(),
        )


def test_split_service_applies_temporal_boundaries_and_normalization():
    frame = pd.DataFrame(
        [
            {
                "user_id": index,
                "hist_item_id": [1, 2, 3],
                "hist_event_type": [1, 2, 3],
                "hist_category": [1, 2, 3],
                "hist_brand": [1, 2, 3],
                "hist_price_bucket": [1, 2, 3],
                "hist_time": [1, 2, 3],
                "target_item_id": 10 + index,
                "target_category": 1,
                "target_brand": 1,
                "target_price_bucket": 1,
                "event_time": 100 + index,
                "label": index % 2,
            }
            for index in range(5)
        ]
    )
    service = SplitService(train_ratio=0.6, val_ratio=0.2, max_history_len=2)

    rows = [service.normalize_row(row) for _, row in service.sort_by_prediction_time(frame).iterrows()]
    splits = service.split_by_time(rows)

    assert [len(splits[name]) for name in ("train", "val", "test")] == [3, 1, 1]
    assert rows[0]["hist_item_id"] == [2, 3]


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
        feature_source="feast",
    )

    assert metadata["feature_source"] == "feast"
    assert metadata["feature_service_name"] == DEFAULT_FEATURE_SERVICE_NAME
    assert metadata["entity_input_path"] == labels_path
    assert metadata["feast_repo_path"] == "/repo"
    assert metadata["feast_offline_root"] == "/features"
    assert metadata["train_rows"] == 3
    assert metadata["val_rows"] == 1
    assert metadata["test_rows"] == 1
    assert metadata["hudi"]["enabled"] is False
    assert (tmp_path / "splits" / "dataset_version_meta.json").exists()
    assert (tmp_path / "splits" / "train.jsonl").exists()
    first_train_row = json.loads(
        (tmp_path / "splits" / "train.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert first_train_row["request_id"] == "req-0"
    assert first_train_row["impression_id"] == "imp-0"


def test_prepare_splits_reads_default_offline_feature_store(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_offline_reader(table, iceberg_catalog_name, iceberg_warehouse):
        captured["table"] = table
        captured["catalog"] = iceberg_catalog_name
        captured["warehouse"] = iceberg_warehouse
        return pd.DataFrame(
            [
                {
                    "impression_id": f"imp-{index}",
                    "request_id": f"req-{index}",
                    "user_id": 7,
                    "hist_item_id": [9, 10],
                    "hist_event_type": [1, 2],
                    "hist_category": [3, 4],
                    "hist_brand": [5, 6],
                    "hist_price_bucket": [7, 8],
                    "hist_time": [1, 2],
                    "target_item_id": 11 + index,
                    "target_category": 22,
                    "target_brand": 33,
                    "target_price_bucket": 44,
                    "event_time": 1767226200 + index,
                    "prediction_timestamp": pd.Timestamp("2026-01-01T00:10:00Z") + pd.Timedelta(minutes=index),
                    "label": index % 2,
                }
                for index in range(5)
            ]
        )

    monkeypatch.setattr(
        "cli.prepare_bst_training_data.build_bst_training_table_from_offline_feature_store",
        fake_offline_reader,
    )

    metadata = prepare_bst_jsonl_splits(
        entity_input_path="ignored-for-offline-feature-store",
        output_dir=tmp_path / "splits",
        train_ratio=0.6,
        val_ratio=0.2,
        max_history_len=2,
    )

    assert captured["table"] == DEFAULT_OFFLINE_FEATURE_TABLE
    assert captured["catalog"] == "recsys_features"
    assert captured["warehouse"] == "s3a://recsys-offline-feature-store/warehouse"
    assert metadata["feature_source"] == "offline_feature_store"
    assert metadata["offline_feature_table"] == DEFAULT_OFFLINE_FEATURE_TABLE
    assert metadata["train_rows"] == 3
    assert metadata["val_rows"] == 1
    assert metadata["test_rows"] == 1


def test_prepare_splits_records_hudi_latency_when_versioning_enabled(monkeypatch, tmp_path):
    def fake_offline_reader(table, iceberg_catalog_name, iceberg_warehouse):
        return pd.DataFrame(
            [
                {
                    "impression_id": f"imp-{index}",
                    "request_id": f"req-{index}",
                    "user_id": 7,
                    "hist_item_id": [9, 10],
                    "hist_event_type": [1, 2],
                    "hist_category": [3, 4],
                    "hist_brand": [5, 6],
                    "hist_price_bucket": [7, 8],
                    "hist_time": [1, 2],
                    "target_item_id": 11 + index,
                    "target_category": 22,
                    "target_brand": 33,
                    "target_price_bucket": 44,
                    "event_time": 1767226200 + index,
                    "prediction_timestamp": pd.Timestamp("2026-01-01T00:10:00Z") + pd.Timedelta(minutes=index),
                    "label": index % 2,
                }
                for index in range(5)
            ]
        )

    def fake_commit_samples_to_hudi(
        samples,
        output_dir,
        dataset_run_id,
        config,
        *,
        processing_code,
        feature_service_version,
    ):
        assert config.dataset_table == "ml.bst_samples_native_v2"
        assert processing_code
        assert feature_service_version == "bst_ranking_v1"
        for split in ("train", "val", "test"):
            (tmp_path / "splits" / f"{split}.jsonl").write_text("", encoding="utf-8")
        return {
            "enabled": True,
            "storage": "hudi",
            "operation": "upsert",
            "table": {
                "name": "recsys_features.ml.bst_samples_native_v2",
                "path": "s3a://warehouse/recsys_features/ml/bst_samples_native_v2",
                "hudi_instant": "001",
                "input_rows": 5,
                "upsert_rows": 5,
                "delete_rows": 0,
                "write_rows": 5,
                "snapshot_rows": 5,
                "split_rows": {"train": 3, "val": 1, "test": 1},
            },
            "jsonl_counts": {"train": 3, "val": 1, "test": 1},
            "latency_ms": {"commit": 19.0, "jsonl_export": 3.0, "total": 22.0},
        }

    monkeypatch.setattr(
        "cli.prepare_bst_training_data.build_bst_training_table_from_offline_feature_store",
        fake_offline_reader,
    )
    monkeypatch.setattr("cli.prepare_bst_training_data.commit_samples_to_hudi", fake_commit_samples_to_hudi)

    metadata = prepare_bst_jsonl_splits(
        entity_input_path="ignored-for-offline-feature-store",
        output_dir=tmp_path / "splits",
        train_ratio=0.6,
        val_ratio=0.2,
        max_history_len=2,
        hudi_enabled=True,
    )

    assert metadata["hudi"]["storage"] == "hudi"
    assert metadata["versioning_latency_ms"]["total"] == 22.0
    assert metadata["hudi"]["table"]["hudi_instant"] == "001"
    dataset_metadata = json.loads(
        (tmp_path / "splits" / "dataset_version_meta.json").read_text(encoding="utf-8")
    )
    assert {
        dataset_metadata["splits"][split]["hudi_instant"]
        for split in ("train", "val", "test")
    } == {"001"}


def test_versioned_samples_use_composite_key_and_split_routes():
    row = _versioned_row("imp-1", 11)
    samples = to_versioned_samples(
        {
            "train": [row],
            "val": [],
            "test": [_versioned_row("imp-2", 11)],
        },
    )

    assert samples.groupby("split").size().to_dict() == {"test": 1, "train": 1}
    assert "sample_id" not in samples
    assert "row_hash" not in samples
    assert set(samples["_hoodie_is_deleted"]) == {False}
    assert str(samples["source_updated_at"].dt.tz) == "UTC"


def test_versioned_samples_reject_null_duplicate_and_empty_keys():
    with pytest.raises(ValueError, match="non-null"):
        to_versioned_samples(
            {"train": [_versioned_row("", 11)], "val": [], "test": []}
        )

    duplicate = _versioned_row("imp-1", 11)
    with pytest.raises(ValueError, match="unique"):
        to_versioned_samples(
            {"train": [duplicate], "val": [dict(duplicate)], "test": []}
        )

    with pytest.raises(ValueError, match="empty source snapshot"):
        to_versioned_samples({"train": [], "val": [], "test": []})


def test_spark_safe_records_convert_timezone_aware_timestamps():
    samples = pd.DataFrame(
        [
            {
                "impression_id": "imp-1",
                "request_id": "req-1",
                "user_id": 7,
                "target_item_id": 11,
                "event_timestamp": pd.Timestamp("2026-01-01T00:10:00Z"),
                "source_updated_at": pd.Timestamp("2026-01-01T00:12:00Z"),
                "split": "train",
                "_hoodie_is_deleted": False,
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
            }
        ]
    )

    record = _spark_safe_records(samples)[0]

    assert record["event_timestamp"].tzinfo is None
    assert record["source_updated_at"].tzinfo is None
