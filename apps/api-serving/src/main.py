from __future__ import annotations

import asyncio
import logging
import os
import time

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response, status

from observability import configure_logging, configure_tracing, log_event, metrics_text, observe_request
from ab_testing import TritonABRouter
from online_features import FeatureClient, get_online_features
from ranking import recommend
from api_schemas import OnlineFeaturesResponse, RecommendationRequest, RecommendationResponse


app = FastAPI(title="RecSys API Serving", version="0.1.0")
configure_logging()
configure_tracing(app)
_feature_client: FeatureClient | None = None
_ranker: TritonABRouter | None = None


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    route = request.scope.get("path", request.url.path)
    method = request.method
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception as exc:
        log_event(
            "api request failed",
            logging.ERROR,
            component="api",
            route=route,
            method=method,
            status=status_code,
            error_type=exc.__class__.__name__,
        )
        raise
    finally:
        duration = time.perf_counter() - start
        observe_request(route, method, status_code, duration)
        log_event(
            "api request completed",
            component="api",
            route=route,
            method=method,
            status=status_code,
            duration_ms=round(duration * 1000, 3),
            model_version=os.getenv("MODEL_VERSION", "latest"),
        )


def feature_client() -> FeatureClient:
    global _feature_client
    if _feature_client is None:
        _feature_client = FeatureClient()
    return _feature_client


def ranker() -> TritonABRouter:
    global _ranker
    if _ranker is None:
        _ranker = TritonABRouter.from_env()
    return _ranker


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    if os.getenv("FORCE_NOT_READY") == "1":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="forced not ready")
    return {"status": "ready"}


@app.get("/version")
async def version() -> dict[str, str]:
    return {
        "service": "recsys-api-serving",
        "model_version": os.getenv("MODEL_VERSION", "latest"),
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(metrics_text(), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.get("/online-features/{user_id}", response_model=OnlineFeaturesResponse)
async def online_features(
    user_id: int = Path(ge=1),
    candidate_item_ids: list[int] | None = Query(default=None),
    top_k: int = Query(default=10, ge=1, le=100),
) -> OnlineFeaturesResponse:
    try:
        return await asyncio.to_thread(
            get_online_features,
            user_id=user_id,
            candidate_item_ids=candidate_item_ids,
            top_k=top_k,
            feature_client=feature_client(),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"online feature fetch failed: {exc}") from exc


@app.post("/recommendations", response_model=RecommendationResponse)
async def recommendations(request: RecommendationRequest) -> RecommendationResponse:
    try:
        return await asyncio.to_thread(
            recommend,
            request=request,
            feature_client=feature_client(),
            ranker=ranker(),
            model_version=os.getenv("MODEL_VERSION", "latest"),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"inference failed: {exc}") from exc
