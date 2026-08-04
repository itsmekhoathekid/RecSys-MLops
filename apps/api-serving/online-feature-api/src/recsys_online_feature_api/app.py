from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Path, Query, Request

from recsys_serving_common.contracts import (
    OnlineFeaturesRequest,
    OnlineFeaturesResponse,
)
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
            await asyncio.to_thread(client._feature_store)
        try:
            yield
        finally:
            client.close()

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
            return await asyncio.to_thread(
                get_online_features,
                user_id=payload.user_id,
                candidate_item_ids=payload.candidate_item_ids,
                top_k=payload.top_k,
                feature_client=request.app.state.feature_client,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"online feature fetch failed: {exc}"
            ) from exc

    @app.get("/online-features/{user_id}", response_model=OnlineFeaturesResponse)
    async def online_features_get(
        request: Request,
        user_id: int = Path(ge=1),
        candidate_item_ids: list[int] | None = Query(default=None),
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
