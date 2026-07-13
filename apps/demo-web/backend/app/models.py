from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class User(BaseModel):
    user_id: int
    segment: str | None = None
    city: str | None = None


class Product(BaseModel):
    product_id: int
    product_name: str
    category_id: int
    category_code: str | None = None
    brand_id: int
    brand_name: str | None = None
    current_price: float
    price_bucket: int


class UserPage(BaseModel):
    items: list[User]
    total: int
    limit: int
    offset: int


class ProductPage(BaseModel):
    items: list[Product]
    total: int
    limit: int
    offset: int


class EventRequest(BaseModel):
    user_id: int = Field(ge=1)
    product_id: int = Field(ge=1)
    action: Literal["view", "cart", "purchase"]
    session_id: str = Field(min_length=1, max_length=160)
    request_id: str | None = Field(default=None, max_length=160)
    impression_id: str | None = Field(default=None, max_length=160)
    quantity: int = Field(default=1, ge=1, le=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)

    @field_validator("session_id", "request_id", "impression_id", "idempotency_key")
    @classmethod
    def strip_identifiers(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("identifier must not be blank")
        return stripped


class EventAccepted(BaseModel):
    event_id: str
    correlation_id: str
    status: Literal["accepted"] = "accepted"
    duplicate: bool = False
    event_timestamp: datetime


class EventStatus(BaseModel):
    event_id: str
    status: Literal["accepted", "feature_store_updated"]
    feature_service_available: bool = True


class RecommendationRequest(BaseModel):
    user_id: int = Field(ge=1)
    top_k: int = Field(default=10, ge=1, le=50)
    session_id: str | None = Field(default=None, min_length=1, max_length=160)


class RecommendationItem(BaseModel):
    item_id: int
    score: float
    impression_id: str
    product: Product | None = None


class RecommendationResponse(BaseModel):
    request_id: str
    user_id: int
    model_version: str
    ab_variant: str | None = None
    ab_experiment_id: str | None = None
    items: list[RecommendationItem]
