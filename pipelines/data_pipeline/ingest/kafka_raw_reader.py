from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class KafkaTopicContract:
    logical_table: str
    topic: str
    key_field: str
    event_time_field: str | None


CDC_TOPIC_CONTRACTS: tuple[KafkaTopicContract, ...] = (
    KafkaTopicContract("users", "cdc.users", "user_id", "updated_ts"),
    KafkaTopicContract("user_preferences", "cdc.user_preferences", "user_id", "updated_ts"),
    KafkaTopicContract("products", "cdc.products", "product_id", "updated_ts"),
    KafkaTopicContract("product_snapshots", "cdc.product_snapshots", "product_id", "valid_from"),
    KafkaTopicContract("sessions", "cdc.sessions", "session_id", "session_start_ts"),
    KafkaTopicContract("recommendation_requests", "cdc.recommendation_requests", "request_id", "request_timestamp"),
    KafkaTopicContract("impressions", "cdc.impressions", "impression_id", "impression_timestamp"),
    KafkaTopicContract("behavior_events", "cdc.behavior_events", "event_id", "event_timestamp"),
    KafkaTopicContract("orders", "cdc.orders", "order_id", "order_timestamp"),
    KafkaTopicContract("order_items", "cdc.order_items", "order_item_id", "created_ts"),
)


def contracts_by_topic(
    contracts: Iterable[KafkaTopicContract] = CDC_TOPIC_CONTRACTS,
) -> dict[str, KafkaTopicContract]:
    return {contract.topic: contract for contract in contracts}


def required_topics() -> list[str]:
    return [contract.topic for contract in CDC_TOPIC_CONTRACTS]

