from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq

from domain import RecordMixin
from schemas import PARTITION_FIELDS, SCHEMAS


COMPATIBLE_SCHEMA_EVOLUTION_COLUMNS = {
    "behavior_events": {
        1: ("device_type", "campaign_id"),
    },
}


def _arrow_value(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (datetime, date)):
        return value
    return value


def record_to_arrow_dict(record: RecordMixin) -> dict[str, Any]:
    return {key: _arrow_value(value) for key, value in record.to_dict().items()}


def physical_schema_for_records(
    table_name: str,
    records: list[RecordMixin],
    schema: pa.Schema,
) -> pa.Schema:
    """Return the physical Parquet schema for one generator partition.

    V1 behavior-event files intentionally omit the columns introduced in V2.
    Later files retain the complete contract, giving Spark real compatible
    Parquet schemas to merge instead of representing evolution with nulls only.
    """
    version_columns = COMPATIBLE_SCHEMA_EVOLUTION_COLUMNS.get(table_name)
    if not version_columns or not records:
        return schema
    versions = {int(getattr(record, "schema_version", 1)) for record in records}
    if versions != {1}:
        return schema
    omitted = set(version_columns[1])
    return pa.schema(field for field in schema if field.name not in omitted)


class LocalParquetSink:
    def __init__(self, run_path: Path, overwrite: bool = False):
        self.run_path = run_path
        if run_path.exists():
            if not overwrite:
                raise FileExistsError(f"Output already exists: {run_path}")
            shutil.rmtree(run_path)
        run_path.mkdir(parents=True, exist_ok=True)

    def write(self, table_name: str, records: list[RecordMixin]) -> list[str]:
        schema = SCHEMAS[table_name]
        table_path = self.run_path / table_name
        table_path.mkdir(parents=True, exist_ok=True)
        if not records:
            empty = pa.Table.from_pylist([], schema=schema)
            output = table_path / "part-00000.parquet"
            pq.write_table(empty, output)
            return [str(output)]

        partition_field = PARTITION_FIELDS.get(table_name)
        if partition_field is None:
            output = table_path / "part-00000.parquet"
            self._write_file(output, records, physical_schema_for_records(table_name, records, schema))
            return [str(output)]

        grouped: dict[str, list[RecordMixin]] = defaultdict(list)
        for record in records:
            value = getattr(record, partition_field)
            partition_date = value.date() if isinstance(value, datetime) else value
            grouped[str(partition_date)].append(record)

        outputs: list[str] = []
        for partition_date, partition_records in sorted(grouped.items()):
            directory = table_path / f"business_date={partition_date}"
            directory.mkdir(parents=True, exist_ok=True)
            output = directory / "part-00000.parquet"
            partition_schema = physical_schema_for_records(table_name, partition_records, schema)
            self._write_file(output, partition_records, partition_schema)
            outputs.append(str(output))
        return outputs

    @staticmethod
    def _write_file(
        output: Path, records: list[RecordMixin], schema: pa.Schema
    ) -> None:
        table = pa.Table.from_pylist(
            [record_to_arrow_dict(record) for record in records],
            schema=schema,
        )
        pq.write_table(table, output, compression="zstd")

    def write_json(self, filename: str, payload: dict[str, Any]) -> Path:
        path = self.run_path / filename
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return path


def read_table(run_path: Path, table_name: str) -> pa.Table:
    files = sorted((run_path / table_name).rglob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"No parquet files for table {table_name}")
    # ParquetFile avoids Hive partition discovery adding a synthetic
    # `business_date` column that is not part of the table contract.
    merged = pa.concat_tables(
        [pq.ParquetFile(path).read() for path in files],
        promote_options="default",
    )
    expected_schema = SCHEMAS[table_name]
    for field in expected_schema:
        if field.name not in merged.column_names:
            merged = merged.append_column(
                field.name,
                pa.nulls(merged.num_rows, type=field.type),
            )
    return merged.select(expected_schema.names).cast(expected_schema)
