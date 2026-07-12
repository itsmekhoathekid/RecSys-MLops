from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException

from ab_testing import TritonABRouter, select_triton_route
from api_runtime import configure_api, healthz, metrics, ready, version_payload
from api_schemas import OnlineFeaturesRequest, RecommendationRequest, RecommendationResponse
from feature_service_client import OnlineFeatureServiceClient
from observability import METRICS, observe_model_prediction
from ranking import recommend_from_online_features
from serving_utils import ab_labels
from shadow import ShadowRunner


app = configure_api(FastAPI(title="RecSys Inference API", version="0.1.0"))
_feature_service_client: OnlineFeatureServiceClient | None = None
_ranker: TritonABRouter | None = None
_shadow_runner: ShadowRunner | None = None


def feature_service_client() -> OnlineFeatureServiceClient:
    global _feature_service_client
    if _feature_service_client is None:
        _feature_service_client = OnlineFeatureServiceClient()
    return _feature_service_client


def ranker() -> TritonABRouter:
    global _ranker
    if _ranker is None:
        _ranker = TritonABRouter.from_env()
    return _ranker


def shadow_runner() -> ShadowRunner:
    global _shadow_runner
    if _shadow_runner is None:
        _shadow_runner = ShadowRunner(
            timeout_seconds=max(1, int(os.getenv("AB_SHADOW_TIMEOUT_MS", "1000"))) / 1000,
            max_pending=max(1, int(os.getenv("AB_SHADOW_QUEUE_SIZE", "100"))),
            max_concurrency=max(1, int(os.getenv("AB_SHADOW_MAX_CONCURRENCY", "4"))),
        )
    return _shadow_runner


@app.get("/healthz")
async def inference_healthz() -> dict[str, str]:
    return await healthz()


@app.get("/ready")
async def inference_ready() -> dict[str, str]:
    return await ready()


@app.get("/version")
async def version() -> dict[str, object]:
    return version_payload(
        "recsys-api-serving",
        feature_api_url=os.getenv("FEATURE_API_URL", "http://recsys-online-feature-api"),
        inference_engine="Triton Inference Server",
    )


@app.get("/metrics")
async def inference_metrics():
    # Prometheus must see rollout configuration even before the first user request.
    ranker()
    return await metrics()


@app.post("/recommendations", response_model=RecommendationResponse)
async def recommendations(request: RecommendationRequest) -> RecommendationResponse:
    router = ranker()
    route = select_triton_route(router, request.user_id, os.getenv("MODEL_VERSION", "latest"))
    shadow_route = router.shadow_route(request.user_id) if hasattr(router, "shadow_route") else None
    metric_labels = ab_labels(route.ab_variant, route.model_version, route.ab_experiment_id)
    start = time.perf_counter()
    status = "error"
    confidence: float | None = None
    try:
        online_features = await feature_service_client().fetch(
            OnlineFeaturesRequest(
                user_id=request.user_id,
                candidate_item_ids=request.candidate_item_ids,
                top_k=request.top_k,
            )
        )
        response = recommend_from_online_features(
            online_features=online_features,
            top_k=request.top_k,
            route=route,
            metric_labels=metric_labels,
            payload_observer=(
                (lambda payload: shadow_runner().submit(shadow_route, payload)) if shadow_route is not None else None
            ),
        )
        status = "success" if response.items else "empty"
        if response.items:
            confidence = max(item.score for item in response.items)
        return response
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"inference failed: {exc}") from exc
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
        METRICS.observe("recsys_api_recommendation_duration_seconds", duration, labels=metric_labels)


@app.on_event("shutdown")
async def drain_shadow_inferences() -> None:
    if _shadow_runner is not None:
        await _shadow_runner.drain()
