from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request

from recsys_serving_common.contracts import OnlineFeaturesRequest
from recsys_serving_common.observability import METRICS, observe_model_prediction
from recsys_serving_common.runtime import (
    configure_api,
    healthz,
    metrics,
    ready,
    version_payload,
)
from recsys_inference_api.ab_testing import TritonABRouter, select_triton_route
from recsys_inference_api.feature_client import OnlineFeatureServiceClient
from recsys_inference_api.labels import ab_labels
from recsys_inference_api.ranking import recommend_from_online_features
from recsys_inference_api.schemas import RecommendationRequest, RecommendationResponse
from recsys_inference_api.settings import InferenceApiSettings
from recsys_inference_api.shadow import ShadowRunner


def create_app(
    settings: InferenceApiSettings | None = None,
    feature_service: OnlineFeatureServiceClient | None = None,
    router: TritonABRouter | None = None,
    shadow_runner: ShadowRunner | None = None,
) -> FastAPI:
    settings = settings or InferenceApiSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        http_client: httpx.AsyncClient | None = None
        service = feature_service
        if service is None:
            http_client = httpx.AsyncClient(
                timeout=settings.feature_api_timeout_seconds
            )
            service = OnlineFeatureServiceClient(
                base_url=settings.feature_api_url,
                timeout_seconds=settings.feature_api_timeout_seconds,
                client=http_client,
            )
        app.state.feature_service = service
        app.state.ranker = router or TritonABRouter.from_env()
        app.state.shadow_runner = shadow_runner or ShadowRunner(
            timeout_seconds=settings.shadow_timeout_seconds,
            max_pending=settings.shadow_queue_size,
            max_concurrency=settings.shadow_max_concurrency,
        )
        try:
            yield
        finally:
            await app.state.shadow_runner.drain()
            if http_client is not None:
                await http_client.aclose()

    app = configure_api(
        FastAPI(title="RecSys Inference API", version="0.1.0", lifespan=lifespan)
    )

    @app.get("/healthz")
    async def inference_healthz() -> dict[str, str]:
        return await healthz()

    @app.get("/ready")
    async def inference_ready() -> dict[str, str]:
        return await ready()

    @app.get("/version")
    async def version() -> dict[str, object]:
        return version_payload(
            "recsys-inference-api",
            model_version=settings.model_version,
            feature_api_url=settings.feature_api_url,
            inference_engine="Triton Inference Server",
        )

    @app.get("/metrics")
    async def inference_metrics():
        return await metrics()

    @app.post("/recommendations", response_model=RecommendationResponse)
    async def recommendations(
        payload: RecommendationRequest, request: Request
    ) -> RecommendationResponse:
        active_router = request.app.state.ranker
        route = select_triton_route(
            active_router, payload.user_id, settings.model_version
        )
        shadow_route = (
            active_router.shadow_route(payload.user_id)
            if hasattr(active_router, "shadow_route")
            else None
        )
        metric_labels = ab_labels(
            route.ab_variant, route.model_version, route.ab_experiment_id
        )
        start = time.perf_counter()
        status = "error"
        confidence: float | None = None
        try:
            online_features = await request.app.state.feature_service.fetch(
                OnlineFeaturesRequest(
                    user_id=payload.user_id,
                    candidate_item_ids=payload.candidate_item_ids,
                    top_k=payload.top_k,
                )
            )
            response = recommend_from_online_features(
                online_features=online_features,
                top_k=payload.top_k,
                route=route,
                metric_labels=metric_labels,
                payload_observer=(
                    lambda triton_payload: (
                        request.app.state.shadow_runner.submit(
                            shadow_route, triton_payload
                        )
                        if shadow_route is not None
                        else None
                    )
                ),
            )
            status = "success" if response.items else "empty"
            if response.items:
                confidence = max(item.score for item in response.items)
            return response
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail=f"inference failed: {exc}"
            ) from exc
        finally:
            duration = time.perf_counter() - start
            observe_model_prediction(
                model_version=route.model_version,
                duration_seconds=duration,
                confidence=confidence,
                status=status,
                labels={
                    "ab_variant": metric_labels["ab_variant"],
                    "experiment_id": metric_labels["experiment_id"],
                },
            )
            METRICS.observe(
                "recsys_api_recommendation_duration_seconds",
                duration,
                labels=metric_labels,
            )

    return app


app = create_app()
