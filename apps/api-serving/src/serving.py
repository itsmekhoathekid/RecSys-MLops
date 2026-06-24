from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from observability import METRICS, observe_redis, observe_triton, span


HISTORY_FIELDS = {
    "hist_item_id": "hist_item_ids",
    "hist_event_type": "hist_event_type_ids",
    "hist_category": "hist_category_ids",
    "hist_brand": "hist_brand_ids",
    "hist_price_bucket": "hist_price_bucket_ids",
    "hist_time": "hist_time_ids",
}

CARDINALITY_ENV = {
    "item": ("MODEL_ITEM_NUM", 22700),
    "category": ("MODEL_CATEGORY_NUM", 30),
    "brand": ("MODEL_BRAND_NUM", 740),
    "price_bucket": ("MODEL_PRICE_BUCKET_NUM", 10),
    "event_type": ("MODEL_EVENT_TYPE_NUM", 3),
    "time": ("MODEL_TIME_BUCKET_NUM", 9),
}


class RecommendationRequest(BaseModel):
    user_id: int = Field(ge=1)
    candidate_item_ids: list[int] | None = Field(default=None, min_length=1, max_length=500)
    top_k: int = Field(default=10, ge=1, le=100)


class RecommendationItem(BaseModel):
    item_id: int
    score: float


class RecommendationResponse(BaseModel):
    user_id: int
    model_version: str
    items: list[RecommendationItem]


class OnlineFeaturesResponse(BaseModel):
    user_id: int
    candidate_item_ids: list[int]
    user_sequence: dict[str, Any]
    item_features: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ItemFeatures:
    item_id: int
    category: int = 0
    brand: int = 0
    price_bucket: int = 0


def parse_json_bytes(value: bytes | str | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not value:
        return {}
    return json.loads(value)


def as_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value) if value.strip().startswith("[") else []
    if not isinstance(value, (list, tuple)):
        return []
    return [int(item) for item in value if item is not None]


def embedding_index(value: Any, kind: str) -> int:
    env_name, default = CARDINALITY_ENV[kind]
    cardinality = max(1, int(os.getenv(env_name, str(default))))
    return max(0, int(value or 0)) % cardinality


def normalize_sequence_features(row: dict[str, Any]) -> dict[str, list[int]]:
    return {
        "hist_item_id": [embedding_index(value, "item") for value in as_int_list(row.get("hist_item_ids"))],
        "hist_event_type": [
            embedding_index(value, "event_type") for value in as_int_list(row.get("hist_event_type_ids"))
        ],
        "hist_category": [
            embedding_index(value, "category") for value in as_int_list(row.get("hist_category_ids"))
        ],
        "hist_brand": [embedding_index(value, "brand") for value in as_int_list(row.get("hist_brand_ids"))],
        "hist_price_bucket": [
            embedding_index(value, "price_bucket") for value in as_int_list(row.get("hist_price_bucket_ids"))
        ],
        "hist_time": [embedding_index(value, "time") for value in as_int_list(row.get("hist_time_ids"))],
    }


def normalize_item_features(item_id: int, row: dict[str, Any] | None) -> ItemFeatures:
    row = row or {}
    return ItemFeatures(
        item_id=embedding_index(item_id, "item"),
        category=embedding_index(row.get("category_id", 0), "category"),
        brand=embedding_index(row.get("brand_id", 0), "brand"),
        price_bucket=embedding_index(row.get("price_bucket", 0), "price_bucket"),
    )


def build_triton_payload(
    sequence_row: dict[str, Any],
    item_rows: dict[int, dict[str, Any]],
    candidate_item_ids: list[int],
) -> dict[str, np.ndarray]:
    sequence = normalize_sequence_features(sequence_row)
    items = [normalize_item_features(item_id, item_rows.get(item_id)) for item_id in candidate_item_ids]
    payload = {
        name: np.asarray(values, dtype=np.int64)
        for name, values in sequence.items()
    }
    payload.update(
        {
            "candidate_item_id": np.asarray([item.item_id for item in items], dtype=np.int64),
            "candidate_category": np.asarray([item.category for item in items], dtype=np.int64),
            "candidate_brand": np.asarray([item.brand for item in items], dtype=np.int64),
            "candidate_price_bucket": np.asarray([item.price_bucket for item in items], dtype=np.int64),
        }
    )
    return payload


def format_top_k(
    user_id: int,
    model_version: str,
    candidate_item_ids: list[int],
    scores: list[float] | np.ndarray,
    top_k: int,
) -> RecommendationResponse:
    pairs = sorted(
        zip(candidate_item_ids, [float(score) for score in scores]),
        key=lambda item: item[1],
        reverse=True,
    )[:top_k]
    return RecommendationResponse(
        user_id=user_id,
        model_version=model_version,
        items=[RecommendationItem(item_id=int(item_id), score=score) for item_id, score in pairs],
    )


class FeatureClient:
    def __init__(self, allow_fallback: bool | None = None) -> None:
        import redis

        if allow_fallback is None:
            allow_fallback = os.getenv("ALLOW_FEATURE_FALLBACK", "0") == "1"
        self.allow_fallback = allow_fallback
        self.client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
        )

    def user_sequence(self, user_id: int) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            with span("redis.user_sequence", operation="user_sequence"):
                payload = parse_json_bytes(self.client.get(f"fs:user_sequence:{user_id}"))
            observe_redis("user_sequence", time.perf_counter() - start)
            if not payload:
                METRICS.inc("recsys_api_empty_feature_total", labels={"feature": "user_sequence"})
            return payload
        except Exception as exc:
            observe_redis("user_sequence", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return {}
            raise RuntimeError(f"failed to fetch user sequence from Redis for user_id={user_id}") from exc

    def item_features(self, item_id: int) -> dict[str, Any]:
        start = time.perf_counter()
        try:
            with span("redis.item_features", operation="item_features"):
                payload = parse_json_bytes(self.client.get(f"fs:item:{item_id}"))
            observe_redis("item_features", time.perf_counter() - start)
            if not payload:
                METRICS.inc("recsys_api_empty_feature_total", labels={"feature": "item_features"})
            return payload
        except Exception as exc:
            observe_redis("item_features", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return {}
            raise RuntimeError(f"failed to fetch item features from Redis for item_id={item_id}") from exc

    def candidates(self, user_id: int, limit: int) -> list[int]:
        start = time.perf_counter()
        try:
            with span("redis.candidates", operation="candidates", limit=limit):
                raw = self.client.zrevrange("candidate:popular:global", 0, max(limit - 1, 0))
            candidates = [int(item.decode("utf-8") if isinstance(item, bytes) else item) for item in raw]
            observe_redis("candidates", time.perf_counter() - start)
            if candidates or self.allow_fallback:
                return candidates
            raise RuntimeError("candidate:popular:global returned no candidates")
        except Exception as exc:
            observe_redis("candidates", time.perf_counter() - start, error=True)
            if self.allow_fallback:
                return list(range(1, limit + 1))
            raise RuntimeError("failed to fetch candidate item IDs from Redis") from exc


class TritonRanker:
    def __init__(self) -> None:
        import tritonclient.grpc as grpcclient

        self.grpcclient = grpcclient
        self.client = grpcclient.InferenceServerClient(url=os.getenv("TRITON_URL", "localhost:8001"))
        self.model_name = os.getenv("TRITON_MODEL_NAME", "bst_ensemble")

    def score(self, payload: dict[str, np.ndarray]) -> tuple[list[int], list[float]]:
        start = time.perf_counter()
        inputs = []
        for name, values in payload.items():
            infer_input = self.grpcclient.InferInput(name, values.shape, "INT64")
            infer_input.set_data_from_numpy(values)
            inputs.append(infer_input)
        outputs = [
            self.grpcclient.InferRequestedOutput("candidate_item_id_out"),
            self.grpcclient.InferRequestedOutput("score"),
        ]
        try:
            with span("triton.infer", model_name=self.model_name, input_count=len(inputs)):
                result = self.client.infer(model_name=self.model_name, inputs=inputs, outputs=outputs)
            item_ids = result.as_numpy("candidate_item_id_out").astype(np.int64).reshape(-1).tolist()
            scores = result.as_numpy("score").astype(np.float32).reshape(-1).tolist()
            observe_triton(self.model_name, time.perf_counter() - start)
            return item_ids, scores
        except Exception:
            observe_triton(self.model_name, time.perf_counter() - start, error=True)
            raise


def recommend(
    request: RecommendationRequest,
    feature_client: FeatureClient,
    ranker: TritonRanker,
    model_version: str,
) -> RecommendationResponse:
    with span("recommend.get_online_features", top_k=request.top_k):
        online_features = get_online_features(
            user_id=request.user_id,
            candidate_item_ids=request.candidate_item_ids,
            top_k=request.top_k,
            feature_client=feature_client,
        )
    candidate_item_ids = online_features.candidate_item_ids
    METRICS.set_gauge("recsys_api_candidate_count", len(candidate_item_ids))
    if not candidate_item_ids:
        METRICS.inc("recsys_api_empty_recommendations_total")
        return RecommendationResponse(user_id=request.user_id, model_version=model_version, items=[])
    sequence_row = online_features.user_sequence
    item_rows = {int(item_id): row for item_id, row in online_features.item_features.items()}
    with span("recommend.build_triton_payload", candidate_count=len(candidate_item_ids)):
        payload = build_triton_payload(sequence_row, item_rows, candidate_item_ids)
    _, scores = ranker.score(payload)
    with span("recommend.format_top_k", top_k=request.top_k):
        response = format_top_k(
            user_id=request.user_id,
            model_version=model_version,
            candidate_item_ids=candidate_item_ids,
            scores=scores,
            top_k=request.top_k,
        )
    METRICS.set_gauge("recsys_api_recommendation_items_count", len(response.items))
    return response


def get_online_features(
    user_id: int,
    candidate_item_ids: list[int] | None,
    top_k: int,
    feature_client: FeatureClient,
) -> OnlineFeaturesResponse:
    candidates = candidate_item_ids or feature_client.candidates(user_id, max(top_k * 5, top_k))
    item_rows = {str(item_id): feature_client.item_features(item_id) for item_id in candidates}
    return OnlineFeaturesResponse(
        user_id=user_id,
        candidate_item_ids=candidates,
        user_sequence=feature_client.user_sequence(user_id),
        item_features=item_rows,
    )
