from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from lineage.dataset_versioning import HudiConfig, _spark_session


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _savepoint_identity(metadata: dict[str, Any]) -> tuple[str, str, str]:
    hudi = metadata.get("hudi", {})
    table = hudi.get("table", {})
    table_name = str(table.get("name") or "")
    table_path = str(table.get("path") or "")
    hudi_instant = str(
        table.get("hudi_instant")
        or table.get("commit_time")
        or table.get("snapshot_id")
        or ""
    )

    if not table_path or not hudi_instant:
        for split in ("train", "val", "test"):
            payload = metadata.get("splits", {}).get(split, {})
            table_name = table_name or str(payload.get("table") or "")
            table_path = table_path or str(payload.get("table_path") or "")
            hudi_instant = hudi_instant or str(
                payload.get("hudi_instant")
                or payload.get("commit_time")
                or payload.get("snapshot_id")
                or ""
            )

    if not table_path:
        raise ValueError("Dataset metadata does not contain the Hudi table path")
    if not hudi_instant:
        raise ValueError("Dataset metadata does not contain a Hudi instant")
    return table_name, table_path, hudi_instant


def _registered_table_name(table_path: str) -> str:
    suffix = hashlib.sha256(table_path.encode("utf-8")).hexdigest()[:12]
    return f"default.recsys_hudi_savepoint_{suffix}"


def _existing_savepoints(spark, registered_table: str) -> set[str]:
    rows = spark.sql(
        f"CALL show_savepoints(table => {_sql_string(registered_table)})"
    ).collect()
    return {str(row[0]) for row in rows}


def create_savepoint(
    metadata: dict[str, Any],
    *,
    user: str = "ml-platform",
    comments: str = "Dataset used by a promoted production model",
    spark=None,
) -> dict[str, Any]:
    table_name, table_path, hudi_instant = _savepoint_identity(metadata)
    owns_spark = spark is None
    spark = spark or _spark_session(HudiConfig())
    registered_table = _registered_table_name(table_path)
    try:
        spark.sql("CREATE DATABASE IF NOT EXISTS default")
        spark.sql(
            f"CREATE TABLE IF NOT EXISTS {registered_table} "
            f"USING hudi LOCATION {_sql_string(table_path)}"
        )
        existing = _existing_savepoints(spark, registered_table)
        if hudi_instant not in existing:
            creation_error: Exception | None = None
            result = []
            try:
                result = spark.sql(
                    "CALL create_savepoint("
                    f"table => {_sql_string(registered_table)}, "
                    f"commit_time => {_sql_string(hudi_instant)}, "
                    f"user => {_sql_string(user)}, "
                    f"comments => {_sql_string(comments)})"
                ).collect()
            except Exception as exc:
                # Another promotion run may create the same savepoint after the
                # first show_savepoints call. Verification below makes that
                # race idempotent without hiding unrelated failures.
                creation_error = exc
            savepoint_visible = (
                hudi_instant
                in _existing_savepoints(spark, registered_table)
            )
            if not savepoint_visible:
                if creation_error is not None:
                    raise creation_error
                raise RuntimeError(
                    f"Hudi savepoint {hudi_instant} was not visible after creation"
                )
            already_existed = creation_error is not None or (
                bool(result) and not bool(result[0][0])
            )
        else:
            already_existed = True

        return {
            "table": table_name,
            "table_path": table_path,
            "hudi_instant": hudi_instant,
            "savepoint_created": not already_existed,
            "already_existed": already_existed,
        }
    finally:
        if owns_spark:
            spark.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Protect a versioned Hudi dataset before model promotion"
    )
    parser.add_argument("--dataset-metadata-path", required=True)
    parser.add_argument("--user", default="ml-platform")
    parser.add_argument(
        "--comments",
        default="Dataset used by a promoted production model",
    )
    parser.add_argument("--output-path", default="")
    args = parser.parse_args()

    metadata = json.loads(
        Path(args.dataset_metadata_path).read_text(encoding="utf-8")
    )
    result = create_savepoint(
        metadata,
        user=args.user,
        comments=args.comments,
    )
    if args.output_path:
        target = Path(args.output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(result, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
