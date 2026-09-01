from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Path, Query, Request

from recsys_serving_common.contracts import (
    OnlineFeaturesRequest,
    OnlineFeaturesResponse,
)
from recsys_serving_common.concurrency import CapacityExceeded
from recsys_serving_common.runtime import (
    configure_api,
    healthz,
    metrics,
    ready,
    version_payload,
)
from recsys_online_feature_api.service import FeatureClient, get_online_features
from recsys_online_feature_api.settings import FeatureApiSettings


def create_app(
    settings: FeatureApiSettings | None = None,
    feature_client: FeatureClient | None = None,
) -> FastAPI:
    settings = settings or FeatureApiSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = feature_client or FeatureClient(settings=settings)
        app.state.feature_client = client
        if settings.warmup_on_startup:
            warmup = getattr(client, "warmup", None)
            if warmup is not None:
                await warmup()
            else:
                client._feature_store()
        try:
            yield
        finally:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
            else:
                sync_close = getattr(client, "close", None)
                if sync_close is not None:
                    sync_close()

    app = configure_api(
        FastAPI(title="RecSys Online Feature API", version="0.1.0", lifespan=lifespan)
    )

    @app.get("/healthz")
    async def feature_healthz() -> dict[str, str]:
        return await healthz()

    @app.get("/ready")
    async def feature_ready() -> dict[str, str]:
        return await ready()

    @app.get("/version")
    async def version() -> dict[str, object]:
        return version_payload(
            "recsys-online-feature-api",
            offline_store="PostgreSQL",
            online_store="Redis",
            feature_store="Feast",
        )

    @app.get("/metrics")
    async def feature_metrics():
        return await metrics()

    @app.post("/online-features", response_model=OnlineFeaturesResponse)
    async def online_features_post(
        payload: OnlineFeaturesRequest, request: Request
    ) -> OnlineFeaturesResponse:
        try:
            return await get_online_features(
                payload.user_id,
                payload.candidate_item_ids,
                payload.top_k,
                request.app.state.feature_client,
            )
        except CapacityExceeded as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"online feature fetch failed: {exc}"
            ) from exc

    @app.get("/online-features/{user_id}", response_model=OnlineFeaturesResponse)
    async def online_features_get(
        request: Request,
        user_id: int = Path(ge=1),
        candidate_item_ids: list[int] | None = Query(
            default=None, min_length=1, max_length=500
        ),
        top_k: int = Query(default=10, ge=1, le=100),
    ) -> OnlineFeaturesResponse:
        return await online_features_post(
            OnlineFeaturesRequest(
                user_id=user_id,
                candidate_item_ids=candidate_item_ids,
                top_k=top_k,
            ),
            request,
        )

    return app


app = create_app()
