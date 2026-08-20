from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

import pytest

from app.database import DemoRepository, IdempotencyConflictError, RecordNotFoundError, event_id_for
from app.models import EventRequest, RecommendationItem

PRODUCT_ROW = {
    "product_id": 101,
    "product_name": "Test Product",
    "category_id": 3,
    "category_code": "cat-3",
    "brand_id": 4,
    "brand_name": "Test Brand",
    "current_price": Decimal("19.99"),
    "price_bucket": 2,
}


class ScriptedCursor:
    def __init__(self, results: list[list[dict] | None]) -> None:
        self.results = list(results)
        self.current: list[dict] = []
        self.executed: list[tuple[str, object]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, query: str, params=None) -> None:
        self.executed.append((" ".join(query.split()), params))
        self.current = (self.results.pop(0) if self.results else None) or []

    async def fetchone(self):
        return self.current[0] if self.current else None

    async def fetchall(self):
        return self.current


class ScriptedConnection:
    def __init__(self, results: list[list[dict] | None]) -> None:
        self.cursor_instance = ScriptedCursor(results)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> ScriptedCursor:
        return self.cursor_instance

    async def commit(self) -> None:
        self.commits += 1


class ScriptedPool:
    def __init__(self, results: list[list[dict] | None]) -> None:
        self.connection_instance = ScriptedConnection(results)
        self.opened = False
        self.closed = False

    async def open(self, wait: bool, timeout: float) -> None:  # noqa: ASYNC109
        self.opened = wait and timeout > 0

    async def close(self) -> None:
        self.closed = True

    @asynccontextmanager
    async def connection(self):
        try:
            yield self.connection_instance
        except Exception:
            self.connection_instance.rollbacks += 1
            raise


def repository(*results: list[dict] | None) -> tuple[DemoRepository, ScriptedPool]:
    pool = ScriptedPool(list(results))
    return DemoRepository(pool=pool), pool  # type: ignore[arg-type]


def request(action: str = "view", **overrides) -> EventRequest:
    payload = {
        "user_id": 1,
        "product_id": 101,
        "action": action,
        "session_id": "session-1",
        "quantity": 2,
    }
    payload.update(overrides)
    return EventRequest.model_validate(payload)


def test_event_id_is_random_without_an_idempotency_key() -> None:
    assert event_id_for(request()) != event_id_for(request())


@pytest.mark.anyio
async def test_pool_lifecycle_ping_and_catalog_queries() -> None:
    repo, pool = repository([{"ok": 1}], [{"total": 1}], [{"user_id": 1, "segment": "vip", "city": "HCMC"}])
    await repo.open()
    await repo.ping()
    users, total = await repo.users(10, 0)
    await repo.close()
    assert pool.opened and pool.closed
    assert total == 1 and users[0].segment == "vip"

    repo, _ = repository([{"total": 1}], [PRODUCT_ROW])
    products, total = await repo.products(10, 0)
    assert total == 1
    assert products[0].current_price == 19.99


@pytest.mark.anyio
async def test_entity_lookup_and_batch_hydration() -> None:
    repo, _ = repository([{"user_id": 1}], [PRODUCT_ROW], [PRODUCT_ROW])
    assert await repo.user_exists(1)
    assert (await repo.product(101)).product_name == "Test Product"
    assert (await repo.products_by_id([101]))[101].brand_name == "Test Brand"
    assert await repo.products_by_id([]) == {}

    repo, pool = repository([], [])
    assert not await repo.user_exists(2)
    assert await repo.product(999) is None


@pytest.mark.anyio
async def test_record_view_and_duplicate_are_idempotent() -> None:
    repo, pool = repository([], [{"user_id": 1}], [PRODUCT_ROW], [], [], [])
    row, duplicate = await repo.record_event(request(), "event-1", "hash-1")
    assert not duplicate
    assert row["request_id"] == "web-event-event-1"
    assert pool.connection_instance.commits == 1
    sql = " ".join(query for query, _ in pool.connection_instance.cursor_instance.executed)
    assert "INSERT INTO sessions" in sql
    assert "INSERT INTO behavior_events" in sql

    existing = {
        "event_id": "event-1",
        "event_timestamp": datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        "request_id": "request-1",
        "payload_hash": "hash-1",
    }
    repo, _ = repository([existing])
    returned, duplicate = await repo.record_event(request(), "event-1", "hash-1")
    assert duplicate and returned == existing

    repo, _ = repository([{**existing, "payload_hash": "different"}])
    with pytest.raises(IdempotencyConflictError):
        await repo.record_event(request(), "event-1", "hash-1")


@pytest.mark.anyio
async def test_record_event_validates_user_and_product() -> None:
    repo, pool = repository([], [])
    with pytest.raises(RecordNotFoundError, match="user"):
        await repo.record_event(request(), "event-1", "hash")
    assert pool.connection_instance.rollbacks == 1

    repo, _ = repository([], [{"user_id": 1}], [])
    with pytest.raises(RecordNotFoundError, match="product"):
        await repo.record_event(request(), "event-1", "hash")


@pytest.mark.anyio
async def test_purchase_is_atomic_and_links_impression() -> None:
    repo, pool = repository([], [{"user_id": 1}], [PRODUCT_ROW], [], [], [], [], [], [], [])
    purchase = request("purchase", request_id="request-1", impression_id="impression-1")
    row, duplicate = await repo.record_event(purchase, "event-purchase", "purchase-hash")
    assert not duplicate and row["request_id"] == "request-1"
    executed = [query for query, _ in pool.connection_instance.cursor_instance.executed]
    assert any("INSERT INTO orders" in query for query in executed)
    assert any("INSERT INTO order_items" in query for query in executed)
    assert any("UPDATE impressions" in query for query in executed)
    assert pool.connection_instance.commits == 1


@pytest.mark.anyio
async def test_event_recommendation_request_and_impressions() -> None:
    event = {
        "event_id": "event-1",
        "event_timestamp": datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        "user_id": 1,
        "product_id": 101,
        "event_type": "view",
        "request_id": "request-1",
        "impression_id": None,
    }
    repo, _ = repository([event])
    assert await repo.event("event-1") == event

    repo, pool = repository([], [])
    await repo.record_recommendation_request(1, "session-1", "request-1")
    assert pool.connection_instance.commits == 1

    item = RecommendationItem(item_id=101, score=0.9, impression_id="impression-1", product=None)
    repo, pool = repository([])
    await repo.record_impressions("request-1", 1, "session-1", [item])
    assert pool.connection_instance.commits == 1
    assert "INSERT INTO impressions" in pool.connection_instance.cursor_instance.executed[0][0]


@pytest.mark.anyio
async def test_repository_propagates_pool_capacity_timeout() -> None:
    class TimeoutPool(ScriptedPool):
        @asynccontextmanager
        async def connection(self):
            if False:
                yield self.connection_instance
            raise TimeoutError("pool capacity exhausted")

    repository = DemoRepository(pool=TimeoutPool([]))  # type: ignore[arg-type]

    with pytest.raises(TimeoutError, match="pool capacity exhausted"):
        await repository.users(1, 0)
