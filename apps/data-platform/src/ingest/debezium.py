from __future__ import annotations

from typing import Any


def extract_debezium_after(record: dict[str, Any]) -> dict[str, Any] | None:
    payload = record.get("payload", record)
    op = payload.get("op")
    if op in {"d", "t"}:
        return None
    after = payload.get("after")
    if after is None and "schema" in record and "payload" in record:
        return None
    if after is None:
        after = payload
    return after

