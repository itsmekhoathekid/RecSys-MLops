from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from lakehouse.iceberg import RAW_GENERATOR_TABLES


@dataclass(frozen=True)
class LakehouseParquetLayout:
    warehouse_uri: str = os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse")
    namespace: str = os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse")

    def table_uri(self, table_name: str) -> str:
        return f"{self.warehouse_uri.rstrip('/')}/{self.namespace}/{table_name}"


def infer_run_id(run_path: str | Path, explicit_run_id: str | None = None) -> str:
    if explicit_run_id:
        return explicit_run_id
    return Path(str(run_path).rstrip("/")).name


def _normalise_uri(uri: str | Path) -> str:
    value = str(uri)
    if value.startswith("s3a://"):
        return "s3://" + value.removeprefix("s3a://")
    return value


def _s3_endpoint() -> tuple[str, str]:
    endpoint = os.getenv("MINIO_ENDPOINT", os.getenv("DATA_PLATFORM_MINIO_ENDPOINT", "http://data-platform-minio:9000"))
    if "://" not in endpoint:
        return "http", endpoint
    parsed = urlparse(endpoint)
    return parsed.scheme, parsed.netloc


def _filesystem_and_path(uri: str | Path) -> tuple[pafs.FileSystem, str]:
    normalised = _normalise_uri(uri)
    parsed = urlparse(normalised)
    if parsed.scheme == "s3":
        scheme, endpoint = _s3_endpoint()
        return (
            pafs.S3FileSystem(
                access_key=os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio")),
                secret_key=os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123")),
                region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                scheme=scheme,
                endpoint_override=endpoint,
            ),
            f"{parsed.netloc}{parsed.path}",
        )
    if parsed.scheme:
        raise ValueError(f"Unsupported lakehouse URI scheme: {parsed.scheme}")
    return pafs.LocalFileSystem(), str(Path(normalised))


def _delete_dir_if_exists(filesystem: pafs.FileSystem, path: str) -> None:
    try:
        filesystem.delete_dir(path)
    except FileNotFoundError:
        return


def _set_column(table: pa.Table, name: str, values: pa.Array) -> pa.Table:
    if name in table.column_names:
        index = table.column_names.index(name)
        return table.set_column(index, name, values)
    return table.append_column(name, values)


def _enrich_table(table: pa.Table, *, source_run_id: str, ingestion_ts: datetime) -> pa.Table:
    run_ids = pa.array([source_run_id] * table.num_rows, type=pa.string())
    timestamps = pa.array([ingestion_ts] * table.num_rows, type=pa.timestamp("us", tz="UTC"))
    enriched = _set_column(table, "source_run_id", run_ids)
    return _set_column(enriched, "lakehouse_ingestion_ts", timestamps)


def _part_name(table_name: str, source_run_id: str) -> str:
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", source_run_id).strip("-") or "run"
    return f"part-{safe_run_id}-{table_name}.parquet"


def _read_parquet_table(table_uri: str) -> pa.Table:
    filesystem, path = _filesystem_and_path(table_uri)
    return pq.read_table(path, filesystem=filesystem)


def _write_parquet_table(table: pa.Table, table_uri: str, *, table_name: str, source_run_id: str, mode: str) -> None:
    filesystem, path = _filesystem_and_path(table_uri)
    if mode == "overwrite":
        _delete_dir_if_exists(filesystem, path)
    filesystem.create_dir(path, recursive=True)
    output_path = f"{path.rstrip('/')}/{_part_name(table_name, source_run_id)}"
    pq.write_table(table, output_path, filesystem=filesystem, compression="snappy")


def load_generator_run_to_lakehouse(
    run_path: str | Path,
    *,
    layout: LakehouseParquetLayout | None = None,
    mode: str = "overwrite",
    run_id: str | None = None,
) -> dict[str, int]:
    layout = layout or LakehouseParquetLayout()
    source_run_id = infer_run_id(run_path, run_id)
    ingestion_ts = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    for table_name in RAW_GENERATOR_TABLES:
        source_uri = f"{str(run_path).rstrip('/')}/{table_name}"
        output_uri = layout.table_uri(table_name)
        table = _read_parquet_table(source_uri)
        enriched = _enrich_table(table, source_run_id=source_run_id, ingestion_ts=ingestion_ts)
        _write_parquet_table(
            enriched,
            output_uri,
            table_name=table_name,
            source_run_id=source_run_id,
            mode=mode,
        )
        counts[table_name] = enriched.num_rows
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest generated batch parquet data into the lakehouse parquet layout")
    parser.add_argument("--run-path", default=os.getenv("GENERATOR_RUN_PATH", "apps/data-platform/data-generator/src/output/test_10k_seed42"))
    parser.add_argument("--run-id", default=os.getenv("GENERATOR_RUN_ID"))
    parser.add_argument("--mode", choices=["append", "overwrite"], default=os.getenv("LAKEHOUSE_INGEST_MODE", "overwrite"))
    parser.add_argument("--lakehouse-warehouse", default=os.getenv("LAKEHOUSE_WAREHOUSE", "s3a://recsys-lakehouse/warehouse"))
    parser.add_argument("--iceberg-lakehouse-namespace", default=os.getenv("ICEBERG_LAKEHOUSE_NAMESPACE", "lakehouse"))
    args = parser.parse_args()
    layout = LakehouseParquetLayout(
        warehouse_uri=args.lakehouse_warehouse,
        namespace=args.iceberg_lakehouse_namespace,
    )
    print(
        json.dumps(
            load_generator_run_to_lakehouse(
                args.run_path,
                layout=layout,
                mode=args.mode,
                run_id=args.run_id,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
