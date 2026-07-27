from __future__ import annotations

import json
import os

import pytest

from cli.create_hudi_savepoint import create_savepoint
from lineage.dataset_versioning import (
    HudiConfig,
    _read_hudi_table,
    _spark_session,
    commit_samples_to_hudi,
    to_versioned_samples,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_HUDI_INTEGRATION") != "1",
    reason="set RUN_HUDI_INTEGRATION=1 and provide the Hudi 1.0.2 Spark bundle",
)


def _row(impression_id: str, target_item_id: int, *, label: int = 1):
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
        "label": label,
    }


def _commit(tmp_path, config, run_id, splits):
    output = tmp_path / run_id
    metadata = commit_samples_to_hudi(
        to_versioned_samples(splits),
        output,
        run_id,
        config,
        processing_code="integration-test",
        feature_service_version="bst_ranking_v1",
    )
    exported = {
        split: [
            json.loads(line)
            for line in (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        for split in ("train", "val", "test")
    }
    return metadata, exported


def test_two_native_upserts_preserve_time_travel_and_one_commit_per_run(tmp_path):
    config = HudiConfig(
        warehouse=(tmp_path / "warehouse").as_uri(),
        dataset_table="ml.bst_samples_native_v2",
        occ_enabled=False,
    )
    snapshot_a = {
        "train": [_row("updated", 11), _row("deleted", 12)],
        "val": [_row("moved", 13)],
        "test": [_row("stable", 14)],
    }
    snapshot_b = {
        "train": [_row("updated", 11, label=0), _row("new-train", 15)],
        "val": [_row("new-val", 16)],
        "test": [_row("moved", 13), _row("stable", 14)],
    }

    commit_a, export_a = _commit(tmp_path, config, "run-a", snapshot_a)
    commit_b, export_b = _commit(tmp_path, config, "run-b", snapshot_b)
    repeated_b, _ = _commit(tmp_path, config, "run-b", snapshot_b)
    instant_a = commit_a["table"]["hudi_instant"]
    instant_b = commit_b["table"]["hudi_instant"]

    assert instant_a != instant_b
    assert commit_a["table"]["delete_rows"] == 0
    assert commit_b["table"]["delete_rows"] == 1
    assert repeated_b["table"]["hudi_instant"] == instant_b
    assert repeated_b["table"]["reused_completed_run"] is True
    assert repeated_b["table"]["write_rows"] == 0
    assert {split: len(rows) for split, rows in export_a.items()} == {
        "train": 2,
        "val": 1,
        "test": 1,
    }
    assert {split: len(rows) for split, rows in export_b.items()} == {
        "train": 2,
        "val": 1,
        "test": 2,
    }

    spark = _spark_session(config)
    try:
        latest = _read_hudi_table(spark, config.table_path)
        historical = _read_hudi_table(spark, config.table_path, instant_a)
        latest_state = {
            (row.impression_id, row.target_item_id, row.split, row.label)
            for row in latest.select(
                "impression_id", "target_item_id", "split", "label"
            ).collect()
        }
        historical_state = {
            (row.impression_id, row.target_item_id, row.split, row.label)
            for row in historical.select(
                "impression_id", "target_item_id", "split", "label"
            ).collect()
        }
        assert latest_state == {
            ("updated", 11, "train", 0),
            ("new-train", 15, "train", 1),
            ("new-val", 16, "val", 1),
            ("moved", 13, "test", 1),
            ("stable", 14, "test", 1),
        }
        assert historical_state == {
            ("updated", 11, "train", 1),
            ("deleted", 12, "train", 1),
            ("moved", 13, "val", 1),
            ("stable", 14, "test", 1),
        }

        meta_client = (
            spark._jvm.org.apache.hudi.common.table.HoodieTableMetaClient.builder()
            .setConf(
                spark._jvm.org.apache.hudi.storage.hadoop.HadoopStorageConfiguration(
                    spark._jsc.hadoopConfiguration()
                )
            )
            .setBasePath(config.table_path)
            .build()
        )
        completed_commits = (
            meta_client.getActiveTimeline()
            .getCommitsTimeline()
            .filterCompletedInstants()
        )
        assert completed_commits.countInstants() == 2

        first_savepoint = create_savepoint(
            {"hudi": commit_b},
            spark=spark,
        )
        repeated_savepoint = create_savepoint(
            {"hudi": commit_b},
            spark=spark,
        )
        assert first_savepoint["savepoint_created"] is True
        assert repeated_savepoint["already_existed"] is True
    finally:
        spark.stop()
