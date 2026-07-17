from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from features.flink.time_utils import isoformat_utc, parse_event_time


@dataclass
class UserSequenceState:
    max_history_length: int = 50
    events_by_user: dict[int, deque[dict[str, Any]]] = field(default_factory=dict)

    def update(self, event: dict[str, Any]) -> dict[str, Any]:
        user_id = int(event["user_id"])
        history = self.events_by_user.setdefault(
            user_id, deque(maxlen=self.max_history_length)
        )
        history.append(event)
        rows = list(history)
        return {
            "user_id": user_id,
            "item_ids": [int(row["product_id"]) for row in rows],
            "event_type_ids": [
                {"view": 1, "cart": 2, "purchase": 3}.get(row["event_type"], 0)
                for row in rows
            ],
            "category_ids": [int(row["category_id"]) for row in rows],
            "brand_ids": [int(row["brand_id"]) for row in rows],
            "price_bucket_ids": [int(row["price_bucket"]) for row in rows],
            "event_timestamps": [row["event_timestamp"] for row in rows],
            "request_ids": [str(row.get("request_id") or "") for row in rows],
            "impression_ids": [str(row.get("impression_id") or "") for row in rows],
            "sequence_length": len(rows),
            "max_history_length": self.max_history_length,
            "updated_at": isoformat_utc(parse_event_time(event["event_timestamp"])),
            "feature_version": "bst_sequence_v2",
        }


def build_user_sequence_payload(event: dict[str, Any], state: UserSequenceState) -> dict[str, Any]:
    return state.update(event)
