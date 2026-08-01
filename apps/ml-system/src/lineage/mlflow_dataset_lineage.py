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
    versions: dict[str, Any] = {}
    for split, payload in splits.items():
        hudi_instant = (
            payload.get("hudi_instant")
            or payload.get("commit_time")
            or payload.get("snapshot_id")
        )
        versions[split] = {
            "table": payload.get("table", ""),
            "table_path": payload.get("table_path", ""),
            "hudi_instant": hudi_instant,
            "snapshot_id": payload.get("snapshot_id"),
            "commit_time": payload.get("commit_time") or hudi_instant,
            "tag": payload.get("tag", ""),
            "row_count": payload.get("row_count", 0),
            "jsonl_path": payload.get("jsonl_path", ""),
        }
    return versions


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
    latency = metadata.get("versioning_latency_ms") or metadata.get("hudi", {}).get("latency_ms", {})
    for name, value in latency.items():
        if value not in {None, ""}:
            mlflow.log_param(f"dataset.versioning_latency_ms.{name}", value)

    splits = metadata.get("splits", {})
    for split, contexts in split_contexts.items():
        payload = splits.get(split)
        if not payload:
            continue
        if isinstance(contexts, str):
            contexts = [contexts]
        for context in contexts:
            prefix = f"dataset.{context}"
            hudi_instant = (
                payload.get("hudi_instant")
                or payload.get("commit_time")
                or payload.get("snapshot_id")
            )
            params = {
                f"{prefix}.split": split,
                f"{prefix}.hudi_table": payload.get("table", ""),
                f"{prefix}.hudi_table_path": payload.get("table_path", ""),
                f"{prefix}.hudi_instant": hudi_instant,
                # Keep the old parameter during the artifact migration.
                f"{prefix}.hudi_commit_time": hudi_instant,
                f"{prefix}.hudi_tag": payload.get("tag", ""),
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
