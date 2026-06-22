from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException

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
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/recommendations", response_model=RecommendationResponse)
def recommendations(request: RecommendationRequest) -> RecommendationResponse:
    try:
        return recommend(
            request=request,
            feature_client=feature_client(),
            ranker=ranker(),
            model_version=os.getenv("MODEL_VERSION", "latest"),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"inference failed: {exc}") from exc
