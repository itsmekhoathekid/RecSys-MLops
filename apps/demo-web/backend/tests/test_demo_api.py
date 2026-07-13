from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app import main, telemetry
from app.database import canonical_payload_hash, event_id_for, event_type_id
from app.models import EventRequest, Product, RecommendationItem, User


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"upstream status {self.status_code}")

    def json(self) -> dict:
        return self._payload


class FakeAsyncClient:
    def __init__(self) -> None:
        self.feature_payload = {
            "user_sequence": {
                "hist_item_ids": [101],
                "hist_event_type_ids": [1],
                "hist_request_ids": ["web-event-event-1"],
                "hist_impression_ids": [""],
            }
        }

    async def get(self, url: str) -> FakeResponse:
        return FakeResponse({"status": "ok"})

    async def post(self, url: str, json: dict) -> FakeResponse:
        if url.endswith("/online-features"):
            return FakeResponse(self.feature_payload)
        return FakeResponse(
            {
                "user_id": json["user_id"],
                "model_version": "stable-001",
                "ab_variant": "control",
                "ab_experiment_id": "demo",
                "items": [{"item_id": 101, "score": 0.91}],
            }
        )


class FakeRepository:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.impressions: list[RecommendationItem] = []
        self.fail_conflict = False

    def ping(self) -> None:
        return None

    def users(self, limit: int, offset: int):
        return [User(user_id=1, segment="vip", city="HCMC")], 1

    def products(self, limit: int, offset: int):
        return [self.product(101)], 1

    def product(self, product_id: int):
        return Product(
            product_id=product_id,
            product_name="Demo Product",
            category_id=3,
            category_code="cat-3",
            brand_id=4,
            brand_name="Demo Brand",
            current_price=19.99,
            price_bucket=2,
        )

    def user_exists(self, user_id: int) -> bool:
        return user_id == 1

    def products_by_id(self, product_ids: list[int]):
        return {product_id: self.product(product_id) for product_id in product_ids}

    def record_event(self, request: EventRequest, event_id: str, payload_hash: str):
        now = datetime.now(UTC)
        row = {
            "event_id": event_id,
            "event_timestamp": now,
            "request_id": request.request_id or f"web-event-{event_id}",
            "user_id": request.user_id,
            "product_id": request.product_id,
            "event_type": request.action,
            "impression_id": request.impression_id,
            "payload_hash": payload_hash,
        }
        duplicate = event_id in self.events
        self.events[event_id] = row
        return row, duplicate

    def event(self, event_id: str):
        return self.events.get(event_id)

    def record_recommendation_request(self, user_id: int, session_id: str, request_id: str) -> None:
        return None

    def record_impressions(self, request_id: str, user_id: int, session_id: str, items: list[RecommendationItem]):
        self.impressions.extend(items)


def event_payload(**overrides):
    payload = {
        "user_id": 1,
        "product_id": 101,
        "action": "view",
        "session_id": "web-session-test",
        "idempotency_key": "event-test-key",
    }
    payload.update(overrides)
    return payload


def test_event_contract_helpers_are_deterministic() -> None:
    request = EventRequest.model_validate(event_payload())
    assert event_type_id("view") == 1
    assert event_type_id("cart") == 2
    assert event_type_id("purchase") == 3
    assert event_id_for(request) == event_id_for(request)
    assert len(canonical_payload_hash(request)) == 64


def test_telemetry_is_optional_and_configures_otlp(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    telemetry.configure_telemetry(main.app)

    processors: list[object] = []

    class Provider:
        def __init__(self, resource) -> None:
            self.resource = resource

        def add_span_processor(self, processor) -> None:
            processors.append(processor)

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://tempo:4317")
    monkeypatch.setattr(telemetry, "TracerProvider", Provider)
    monkeypatch.setattr(telemetry.Resource, "create", lambda attributes: attributes)
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", lambda **kwargs: kwargs)
    monkeypatch.setattr(telemetry, "BatchSpanProcessor", lambda exporter: exporter)
    monkeypatch.setattr(telemetry.trace, "set_tracer_provider", lambda provider: None)
    monkeypatch.setattr(telemetry.FastAPIInstrumentor, "instrument_app", lambda app, tracer_provider: None)

    telemetry.configure_telemetry(main.app)
    assert processors == [{"endpoint": "http://tempo:4317", "insecure": True}]


def test_feature_sequence_matching_uses_full_correlation() -> None:
    event = {
        "product_id": 101,
        "event_type": "view",
        "request_id": "request-1",
        "impression_id": "impression-1",
    }
    sequence = {
        "hist_item_ids": [101],
        "hist_event_type_ids": [1],
        "hist_request_ids": ["request-1"],
        "hist_impression_ids": ["impression-1"],
    }
    assert main._feature_sequence_contains(sequence, event)
    sequence["hist_impression_ids"] = ["different"]
    assert not main._feature_sequence_contains(sequence, event)


def test_users_products_events_status_and_recommendations(monkeypatch) -> None:
    repository = FakeRepository()
    downstream = FakeAsyncClient()
    monkeypatch.setattr(main, "repository", repository)
    monkeypatch.setattr(main, "downstream_client", downstream)
    client = TestClient(main.app)

    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 200
    assert client.get("/api/users").json()["items"][0]["user_id"] == 1
    assert client.get("/api/products").json()["items"][0]["product_id"] == 101

    accepted = client.post("/api/events", json=event_payload()).json()
    assert accepted["status"] == "accepted"
    event_id = accepted["event_id"]
    downstream.feature_payload["user_sequence"]["hist_request_ids"] = [accepted["correlation_id"]]
    status = client.get(f"/api/events/{event_id}/status")
    assert status.json()["status"] == "feature_store_updated"

    recommendation = client.post("/api/recommendations", json={"user_id": 1, "top_k": 1})
    assert recommendation.status_code == 200
    body = recommendation.json()
    assert body["model_version"] == "stable-001"
    assert body["items"][0]["product"]["product_name"] == "Demo Product"
    assert repository.impressions


def test_validation_and_missing_resources(monkeypatch) -> None:
    repository = FakeRepository()
    monkeypatch.setattr(main, "repository", repository)
    monkeypatch.setattr(main, "downstream_client", FakeAsyncClient())
    client = TestClient(main.app)

    assert client.post("/api/events", json=event_payload(action="click")).status_code == 422
    assert client.get("/api/events/missing/status").status_code == 404
    assert client.post("/api/recommendations", json={"user_id": 999, "top_k": 2}).status_code == 404
