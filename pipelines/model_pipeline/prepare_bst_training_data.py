from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from pipelines.data_pipeline.feature_store.offline_writer import read_feature_table


MODEL_COLUMNS = [
    "user_id",
    "hist_item_id",
    "hist_event_type",
    "hist_category",
    "hist_brand",
    "hist_price_bucket",
    "hist_time",
    "target_item_id",
    "target_category",
    "target_brand",
    "target_price_bucket",
    "event_time",
    "label",
]

SEQUENCE_COLUMNS = [
    "hist_item_id",
    "hist_event_type",
    "hist_category",
    "hist_brand",
    "hist_price_bucket",
    "hist_time",
]


def _to_int(value: Any, default: int = 0) -> int:
    if value is None or pd.isna(value):
        return default
    return int(value)


def _to_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, np.ndarray):
        value = value.tolist()
    elif isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else []
    elif not isinstance(value, (list, tuple)):
        return []
    return [int(item) for item in value if item is not None and not pd.isna(item)]


def _normalize_row(row: pd.Series, max_history_len: int) -> dict[str, Any]:
    payload = {column: row.get(column) for column in MODEL_COLUMNS}
    sequences = {column: _to_int_list(payload[column]) for column in SEQUENCE_COLUMNS}
    hist_len = min(
        max((len(values) for values in sequences.values()), default=0),
        max_history_len,
    )
    for column, values in sequences.items():
        values = values[-hist_len:] if hist_len else []
        if len(values) < hist_len:
            values = ([0] * (hist_len - len(values))) + values
        payload[column] = values

    for column in [
        "user_id",
        "target_item_id",
        "target_category",
        "target_brand",
        "target_price_bucket",
        "event_time",
        "label",
    ]:
        payload[column] = _to_int(payload[column])

    return payload


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, separators=(",", ":")) + "\n")


def prepare_bst_jsonl_splits(
    input_path: str,
    output_dir: str | Path,
    train_ratio: float = 0.8,
    val_ratio: float = 0.1,
    max_history_len: int = 50,
) -> dict[str, Any]:
    frame = read_feature_table(input_path)
    if frame.empty:
        raise ValueError(f"No rows found in BST training table: {input_path}")

    if "prediction_timestamp" in frame.columns:
        frame = frame.sort_values("prediction_timestamp")
    else:
        frame = frame.sort_values("event_time")

    rows = [_normalize_row(row, max_history_len=max_history_len) for _, row in frame.iterrows()]
    train_end = int(len(rows) * train_ratio)
    val_end = train_end + int(len(rows) * val_ratio)

    output = Path(output_dir)
    splits = {
        "train": rows[:train_end],
        "val": rows[train_end:val_end],
        "test": rows[val_end:],
    }
    for split, split_rows in splits.items():
        _write_jsonl(split_rows, output / f"{split}.jsonl")

    metadata = {
        "input_path": input_path,
        "output_dir": str(output),
        "total_rows": len(rows),
        "train_rows": len(splits["train"]),
        "val_rows": len(splits["val"]),
        "test_rows": len(splits["test"]),
        "split_strategy": "temporal",
        "max_history_len": max_history_len,
    }
    (output / "split_meta.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare BST JSONL splits from ml_bst_training")
    parser.add_argument(
        "--input-path",
        default="data_pipeline/output/ml/offline/ml_bst_training",
    )
    parser.add_argument("--output-dir", default="notebooks/data/bst_split")
    parser.add_argument("--train-ratio", type=float, default=0.8)
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--max-history-len", type=int, default=50)
    parser.add_argument("--metadata-path", default="")
    args = parser.parse_args()

    metadata = prepare_bst_jsonl_splits(
        input_path=args.input_path,
        output_dir=args.output_dir,
        train_ratio=args.train_ratio,
        val_ratio=args.val_ratio,
        max_history_len=args.max_history_len,
    )
    if args.metadata_path:
        Path(args.metadata_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.metadata_path).write_text(
            json.dumps(metadata, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

