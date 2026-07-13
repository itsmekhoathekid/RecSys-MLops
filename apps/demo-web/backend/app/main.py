from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app.database import (
    DemoRepository,
    IdempotencyConflictError,
    RecordNotFoundError,
    canonical_payload_hash,
    event_id_for,
    event_type_id,
)
from app.models import (
    EventAccepted,
    EventRequest,
    EventStatus,
    ProductPage,
    RecommendationItem,
    RecommendationRequest,
    RecommendationResponse,
    UserPage,
)
from app.telemetry import configure_telemetry

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
LOGGER = logging.getLogger("recsys.demo_api")
REQUESTS = Counter("recsys_demo_api_requests_total", "Demo API requests", ["method", "path", "status"])
LATENCY = Histogram("recsys_demo_api_request_duration_seconds", "Demo API latency", ["method", "path"])

repository = DemoRepository()
downstream_client: httpx.AsyncClient | None = None


def inference_url() -> str:
    return os.getenv("INFERENCE_API_URL", "http://recsys-api-serving.api-serving.svc.cluster.local")


def feature_url() -> str:
    return os.getenv("FEATURE_API_URL", "http://recsys-online-feature-api.api-serving.svc.cluster.local")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global downstream_client
    await asyncio.to_thread(repository.open)
    downstream_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=float(os.getenv("DOWNSTREAM_CONNECT_TIMEOUT_SECONDS", "2")),
            read=float(os.getenv("DOWNSTREAM_READ_TIMEOUT_SECONDS", "15")),
            write=5,
            pool=5,
        ),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    yield
    await downstream_client.aclose()
    await asyncio.to_thread(repository.close)


app = FastAPI(title="RecSys Demo API", version="1.0.0", lifespan=lifespan)
configure_telemetry(app)


@app.middleware("http")
async def observe_requests(request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        REQUESTS.labels(request.method, path, str(status_code)).inc()
        LATENCY.labels(request.method, path).observe(time.perf_counter() - started)
        LOGGER.info(
            "request_complete method=%s path=%s status=%s duration_ms=%.3f",
            request.method,
            path,
            status_code,
            (time.perf_counter() - started) * 1000,
        )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    try:
        await asyncio.to_thread(repository.ping)
        assert downstream_client is not None
        inference, features = await asyncio.gather(
            downstream_client.get(f"{inference_url()}/healthz"),
            downstream_client.get(f"{feature_url()}/healthz"),
        )
        inference.raise_for_status()
        features.raise_for_status()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"dependency not ready: {exc}") from exc
    return {"status": "ready"}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/users", response_model=UserPage)
async def users(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> UserPage:
    try:
        items, total = await asyncio.to_thread(repository.users, limit, offset)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return UserPage(items=items, total=total, limit=limit, offset=offset)


@app.get("/api/products", response_model=ProductPage)
async def products(
    limit: int = Query(default=24, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ProductPage:
    try:
        items, total = await asyncio.to_thread(repository.products, limit, offset)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return ProductPage(items=items, total=total, limit=limit, offset=offset)


@app.post("/api/events", response_model=EventAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_event(
    request: EventRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> EventAccepted:
    selected_key = idempotency_key.strip() if idempotency_key else None
    if selected_key is not None and not 8 <= len(selected_key) <= 200:
        raise HTTPException(status_code=422, detail="Idempotency-Key must contain 8-200 characters")
    event_id = event_id_for(request, selected_key)
    payload_hash = canonical_payload_hash(request)
    try:
        row, duplicate = await asyncio.to_thread(repository.record_event, request, event_id, payload_hash)
    except RecordNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"{exc} not found") from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=f"idempotency conflict for event {exc}") from exc
    except Exception as exc:
        LOGGER.exception("event_write_failed event_id=%s", event_id)
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return EventAccepted(
        event_id=event_id,
        correlation_id=str(row["request_id"]),
        duplicate=duplicate,
        event_timestamp=row["event_timestamp"],
    )


@app.get("/api/events/{event_id}/status", response_model=EventStatus)
async def event_status(event_id: str) -> EventStatus:
    row = await asyncio.to_thread(repository.event, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="event not found")
    try:
        assert downstream_client is not None
        response = await downstream_client.post(
            f"{feature_url()}/online-features",
            json={"user_id": row["user_id"], "candidate_item_ids": [row["product_id"]], "top_k": 1},
        )
        response.raise_for_status()
        sequence = response.json().get("user_sequence") or {}
        if _feature_sequence_contains(sequence, row):
            return EventStatus(event_id=event_id, status="feature_store_updated")
        return EventStatus(event_id=event_id, status="accepted")
    except Exception:
        LOGGER.warning("feature_status_unavailable event_id=%s", event_id, exc_info=True)
        return EventStatus(event_id=event_id, status="accepted", feature_service_available=False)


@app.post("/api/recommendations", response_model=RecommendationResponse)
async def recommendations(request: RecommendationRequest) -> RecommendationResponse:
    try:
        if not await asyncio.to_thread(repository.user_exists, request.user_id):
            raise HTTPException(status_code=404, detail="user not found")
        session_id = request.session_id or f"web-session-{uuid.uuid4()}"
        request_id = f"web-recommendation-{uuid.uuid4()}"
        await asyncio.to_thread(repository.record_recommendation_request, request.user_id, session_id, request_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc

    try:
        assert downstream_client is not None
        upstream = await downstream_client.post(
            f"{inference_url()}/recommendations",
            json={"user_id": request.user_id, "top_k": request.top_k},
        )
        upstream.raise_for_status()
        payload: dict[str, Any] = upstream.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="recommendation service unavailable") from exc

    raw_items = payload.get("items") or []
    product_map = await asyncio.to_thread(repository.products_by_id, [int(item["item_id"]) for item in raw_items])
    items = [
        RecommendationItem(
            item_id=int(item["item_id"]),
            score=float(item["score"]),
            impression_id=f"web-impression-{uuid.uuid4()}",
            product=product_map.get(int(item["item_id"])),
        )
        for item in raw_items
    ]
    try:
        await asyncio.to_thread(repository.record_impressions, request_id, request.user_id, session_id, items)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="failed to persist recommendation impressions") from exc
    return RecommendationResponse(
        request_id=request_id,
        user_id=request.user_id,
        model_version=str(payload.get("model_version", "unknown")),
        ab_variant=payload.get("ab_variant"),
        ab_experiment_id=payload.get("ab_experiment_id"),
        items=items,
    )


def _feature_sequence_contains(sequence: dict[str, Any], event: dict[str, Any]) -> bool:
    item_ids = sequence.get("hist_item_ids") or []
    type_ids = sequence.get("hist_event_type_ids") or []
    request_ids = sequence.get("hist_request_ids") or []
    impression_ids = sequence.get("hist_impression_ids") or []
    expected_type = event_type_id(str(event["event_type"]))
    length = min(len(item_ids), len(type_ids), len(request_ids))
    for index in range(length - 1, -1, -1):
        if int(item_ids[index]) != int(event["product_id"]):
            continue
        if int(type_ids[index]) != expected_type:
            continue
        if str(request_ids[index]) != str(event["request_id"] or ""):
            continue
        if event.get("impression_id") and index < len(impression_ids):
            if str(impression_ids[index]) != str(event["impression_id"]):
                continue
        return True
    return False
