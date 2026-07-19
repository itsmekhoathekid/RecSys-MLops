from __future__ import annotations

import json

from models.dataset import recommenderDataset


def _row(*, request_id: str, impression_id: str, item_id: int, label: int) -> dict:
    return {
        "request_id": request_id,
        "impression_id": impression_id,
        "user_id": 7,
        "hist_item_id": [1],
        "hist_event_type": [1],
        "hist_category": [1],
        "hist_brand": [1],
        "hist_price_bucket": [1],
        "hist_time": [1],
        "target_item_id": item_id,
        "target_category": 1,
        "target_brand": 1,
        "target_price_bucket": 1,
        "event_time": 100,
        "label": label,
    }


def test_collate_preserves_request_as_ranking_group(tmp_path):
    path = tmp_path / "train.jsonl"
    rows = [
        _row(request_id="req-1", impression_id="imp-1", item_id=10, label=1),
        _row(request_id="req-1", impression_id="imp-2", item_id=11, label=0),
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    config = {
        "train_data_path": str(path),
        "val_data_path": str(path),
        "test_data_path": str(path),
        "max_history_len": 2,
        "padding_idx": 0,
    }

    dataset = recommenderDataset(config, split="train")
    batch = dataset.collate_fn([dataset[0], dataset[1]])

    assert batch["ranking_group_id"] == ["req-1", "req-1"]
