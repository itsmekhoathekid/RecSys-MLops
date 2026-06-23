from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException, status

from serving import (
    FeatureClient,
    RecommendationRequest,
    RecommendationResponse,
    TritonRanker,
    recommend,
)


app = FastAPI(title="RecSys API Serving", version="0.1.0")
_feature_client: FeatureClient | None = None
_ranker: TritonRanker | None = None


def feature_client() -> FeatureClient:
    global _feature_client
    if _feature_client is None:
        _feature_client = FeatureClient()
    return _feature_client


def ranker() -> TritonRanker:
    global _ranker
    if _ranker is None:
        _ranker = TritonRanker()
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
