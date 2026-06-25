from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_event_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def isoformat_utc(value: Any) -> str:
    return parse_event_time(value).isoformat()
