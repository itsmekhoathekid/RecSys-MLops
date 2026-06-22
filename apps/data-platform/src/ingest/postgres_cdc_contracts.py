from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourceTableContract:
    table_name: str
    primary_key: tuple[str, ...]
    cdc_topic: str


SOURCE_TABLE_CONTRACTS: tuple[SourceTableContract, ...] = (
    SourceTableContract("users", ("user_id",), "cdc.users"),
    SourceTableContract("user_preferences", ("user_id", "category_id", "brand_id"), "cdc.user_preferences"),
    SourceTableContract("products", ("product_id",), "cdc.products"),
    SourceTableContract("product_snapshots", ("product_id", "valid_from"), "cdc.product_snapshots"),
    SourceTableContract("sessions", ("session_id",), "cdc.sessions"),
    SourceTableContract("recommendation_requests", ("request_id",), "cdc.recommendation_requests"),
    SourceTableContract("impressions", ("impression_id",), "cdc.impressions"),
    SourceTableContract("behavior_events", ("event_id",), "cdc.behavior_events"),
    SourceTableContract("orders", ("order_id",), "cdc.orders"),
    SourceTableContract("order_items", ("order_item_id",), "cdc.order_items"),
)


def primary_keys_by_table() -> dict[str, tuple[str, ...]]:
    return {
        contract.table_name: contract.primary_key
        for contract in SOURCE_TABLE_CONTRACTS
    }

