from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_dataset_metadata(path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    target = Path(path)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8"))


def dataset_versions(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not metadata:
        return {}
    splits = metadata.get("splits", {})
    return {
        split: {
            "table": payload.get("table", ""),
            "snapshot_id": payload.get("snapshot_id"),
            "tag": payload.get("tag", ""),
            "row_count": payload.get("row_count", 0),
            "jsonl_path": payload.get("jsonl_path", ""),
        }
        for split, payload in splits.items()
    }


def log_dataset_lineage(mlflow, metadata: dict[str, Any] | None, split_contexts: dict[str, Any]) -> None:
    if not metadata:
        return
    try:
        import pandas as pd
    except Exception:
        pd = None

    shared_params = {
        "dataset_run_id": metadata.get("dataset_run_id", ""),
        "feast_feature_service": metadata.get("feature_service_name", ""),
        "feast_registry_path": metadata.get("feast_registry_path", ""),
        "entity_input_path": metadata.get("entity_input_path", ""),
        "schema_hash": metadata.get("schema_hash", ""),
        "processing_git_sha": metadata.get("processing_code_version", ""),
        "split_strategy": metadata.get("split_strategy", ""),
    }
    for name, value in shared_params.items():
        if value not in {None, ""}:
            mlflow.log_param(name, value)

    splits = metadata.get("splits", {})
    for split, contexts in split_contexts.items():
        payload = splits.get(split)
        if not payload:
            continue
        if isinstance(contexts, str):
            contexts = [contexts]
        for context in contexts:
            prefix = f"dataset.{context}"
            params = {
                f"{prefix}.split": split,
                f"{prefix}.iceberg_table": payload.get("table", ""),
                f"{prefix}.iceberg_snapshot_id": payload.get("snapshot_id"),
                f"{prefix}.iceberg_tag": payload.get("tag", ""),
                f"{prefix}.row_count": payload.get("row_count", 0),
                f"{prefix}.jsonl_path": payload.get("jsonl_path", ""),
            }
            for key, value in params.items():
                if value not in {None, ""}:
                    mlflow.log_param(key, value)
            input_payload = {**payload, "context": context, "split": split}
            if pd is not None and hasattr(mlflow, "data") and hasattr(mlflow, "log_input"):
                try:
                    frame = pd.DataFrame([input_payload])
                    dataset = mlflow.data.from_pandas(
                        frame,
                        source=payload.get("jsonl_path") or payload.get("table"),
                        name=f"bst_{split}_samples",
                    )
                    mlflow.log_input(dataset, context=context)
                    continue
                except Exception:
                    pass
            mlflow.log_dict(input_payload, f"datasets/{context}.json")

    mlflow.log_dict(metadata, "datasets/dataset_version_meta.json")
