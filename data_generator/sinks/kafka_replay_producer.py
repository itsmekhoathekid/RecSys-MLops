from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from data_generator.sink import read_table


def iter_table_records_ordered(run_path: str | Path, table_name: str, timestamp_column: str) -> Iterable[dict[str, Any]]:
    rows = read_table(Path(run_path), table_name).to_pylist()
    yield from sorted(rows, key=lambda row: str(row.get(timestamp_column)))


def replay_table_to_kafka(
    run_path: str | Path,
    table_name: str,
    topic: str,
    producer: Any,
    key_field: str,
    timestamp_column: str,
) -> int:
    count = 0
    for record in iter_table_records_ordered(run_path, table_name, timestamp_column):
        producer.send(
            topic,
            key=str(record[key_field]).encode("utf-8"),
            value=json.dumps(record, default=str).encode("utf-8"),
        )
        count += 1
    producer.flush()
    return count

