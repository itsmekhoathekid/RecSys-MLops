from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


class RecordMixin:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class User(RecordMixin):
    user_id: int
    signup_ts: datetime
    signup_channel: str
    city: str
    country: str
    segment: str
    age_bucket: int
    preferred_category_id: int
    preferred_brand_id: int
    price_sensitivity: float
    user_lifecycle_state: str
    last_active_ts: datetime
    is_active: bool
    created_ts: datetime
    updated_ts: datetime


@dataclass(frozen=True)
class UserPreference(RecordMixin):
    user_id: int
    category_id: int
    brand_id: int | None
    preference_weight: float
    source: str
    created_ts: datetime
    updated_ts: datetime


@dataclass(frozen=True)
class Product(RecordMixin):
    product_id: int
    product_name: str
    category_id: int
    category_code: str
    brand_id: int
    brand_name: str
    base_price: Decimal
    current_price: Decimal
    price_bucket: int
    popularity_weight: float
    is_active: bool
    created_ts: datetime
    updated_ts: datetime


@dataclass(frozen=True)
class ProductSnapshot(RecordMixin):
    product_id: int
    valid_from: datetime
    valid_to: datetime | None
    category_id: int
    category_code: str
    brand_id: int
    brand_name: str
    current_price: Decimal
    price_bucket: int
    is_active: bool
    created_ts: datetime


@dataclass(frozen=True)
class Session(RecordMixin):
    session_id: UUID
    user_id: int
    session_start_ts: datetime
    session_end_ts: datetime
    entry_source: str
    device_type: str
    campaign_id: str | None
    session_end_reason: str
    created_ts: datetime
    updated_ts: datetime


@dataclass(frozen=True)
class RecommendationRequest(RecordMixin):
    request_id: UUID
    user_id: int
    session_id: UUID
    request_timestamp: datetime
    surface: str
    context_product_id: int | None
    context_category_id: int | None
    device_type: str | None
    source: str
    campaign_id: str | None
    created_ts: datetime
    schema_version: int


@dataclass(frozen=True)
class Impression(RecordMixin):
    impression_id: UUID
    request_id: UUID
    user_id: int
    session_id: UUID
    impression_timestamp: datetime
    candidate_product_id: int
    rank_position: int
    candidate_source: str
    retrieval_score: float
    ranking_score: float
    surface: str
    is_clicked: bool
    created_ts: datetime
    schema_version: int


@dataclass(frozen=True)
class BehaviorEvent(RecordMixin):
    event_id: UUID
    event_timestamp: datetime
    created_ts: datetime
    ingestion_ts: datetime
    user_id: int
    session_id: UUID
    request_id: UUID | None
    impression_id: UUID | None
    event_type: str
    product_id: int
    category_id: int
    brand_id: int
    price: Decimal
    price_bucket: int
    quantity: int
    device_type: str | None
    source: str
    campaign_id: str | None
    page_context: str | None
    rank_position: int | None
    order_id: UUID | None
    payload_hash: str
    event_date: date
    schema_version: int
    drift_enabled: bool
    drift_scenario: str | None
    drift_phase: str
    drift_factor: float


@dataclass(frozen=True)
class Order(RecordMixin):
    order_id: UUID
    user_id: int
    session_id: UUID
    order_timestamp: datetime
    status: str
    gross_amount: Decimal
    discount_amount: Decimal
    net_amount: Decimal
    coupon_code: str | None
    payment_method: str | None
    shipping_city: str | None
    paid_ts: datetime | None
    cancelled_ts: datetime | None
    refunded_ts: datetime | None
    created_ts: datetime
    updated_ts: datetime
    drift_enabled: bool
    drift_scenario: str | None
    drift_phase: str
    drift_factor: float


@dataclass(frozen=True)
class OrderItem(RecordMixin):
    order_item_id: UUID
    order_id: UUID
    product_id: int
    quantity: int
    unit_price: Decimal
    discount_amount: Decimal
    line_amount: Decimal
    created_ts: datetime


@dataclass
class GeneratedData:
    users: list[User]
    user_preferences: list[UserPreference]
    products: list[Product]
    product_snapshots: list[ProductSnapshot]
    sessions: list[Session]
    recommendation_requests: list[RecommendationRequest]
    impressions: list[Impression]
    behavior_events: list[BehaviorEvent]
    orders: list[Order]
    order_items: list[OrderItem]

    def table_records(self) -> dict[str, list[RecordMixin]]:
        return {
            "users": self.users,
            "user_preferences": self.user_preferences,
            "products": self.products,
            "product_snapshots": self.product_snapshots,
            "sessions": self.sessions,
            "recommendation_requests": self.recommendation_requests,
            "impressions": self.impressions,
            "behavior_events": self.behavior_events,
            "orders": self.orders,
            "order_items": self.order_items,
        }
