from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Path, Query

from api_runtime import configure_api, healthz, metrics, ready, version_payload
from api_schemas import OnlineFeaturesRequest, OnlineFeaturesResponse
from online_features import FeatureClient, get_online_features


app = configure_api(FastAPI(title="RecSys Online Feature API", version="0.1.0"))
_feature_client: FeatureClient | None = None


def feature_client() -> FeatureClient:
    global _feature_client
    if _feature_client is None:
        _feature_client = FeatureClient()
    return _feature_client


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
        offline_store="Apache Iceberg",
        online_store="Redis",
        feature_store="Feast",
    )


@app.get("/metrics")
async def feature_metrics():
    return await metrics()


@app.post("/online-features", response_model=OnlineFeaturesResponse)
async def online_features_post(request: OnlineFeaturesRequest) -> OnlineFeaturesResponse:
    try:
        return await asyncio.to_thread(
            get_online_features,
            user_id=request.user_id,
            candidate_item_ids=request.candidate_item_ids,
            top_k=request.top_k,
            feature_client=feature_client(),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"online feature fetch failed: {exc}") from exc


@app.get("/online-features/{user_id}", response_model=OnlineFeaturesResponse)
async def online_features_get(
    user_id: int = Path(ge=1),
    candidate_item_ids: list[int] | None = Query(default=None),
    top_k: int = Query(default=10, ge=1, le=100),
) -> OnlineFeaturesResponse:
    return await online_features_post(
        OnlineFeaturesRequest(user_id=user_id, candidate_item_ids=candidate_item_ids, top_k=top_k)
    )
