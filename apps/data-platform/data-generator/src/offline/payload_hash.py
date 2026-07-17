from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from domain import BehaviorEvent


def event_payload_hash(event: BehaviorEvent) -> str:
    payload = {
        "event_id": str(event.event_id),
        "user_id": event.user_id,
        "session_id": str(event.session_id),
        "request_id": str(event.request_id) if event.request_id else None,
        "impression_id": str(event.impression_id) if event.impression_id else None,
        "event_type": event.event_type,
        "product_id": event.product_id,
        "category_id": event.category_id,
        "brand_id": event.brand_id,
        "price": str(event.price),
        "price_bucket": event.price_bucket,
        "quantity": event.quantity,
        "device_type": event.device_type,
        "source": event.source,
        "campaign_id": event.campaign_id,
        "page_context": event.page_context,
        "rank_position": event.rank_position,
        "order_id": str(event.order_id) if event.order_id else None,
        "schema_version": event.schema_version,
        "drift_enabled": event.drift_enabled,
        "drift_scenario": event.drift_scenario,
        "drift_phase": event.drift_phase,
        "drift_factor": event.drift_factor,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class PayloadHashProblem:
    """Recompute the identity hash after the preceding mutations."""

    def apply(self, event: BehaviorEvent) -> BehaviorEvent:
        event = replace(event, payload_hash="")
        return replace(event, payload_hash=event_payload_hash(event))
