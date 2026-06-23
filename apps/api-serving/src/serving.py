from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydantic import BaseModel, Field


HISTORY_FIELDS = {
    "hist_item_id": "hist_item_ids",
    "hist_event_type": "hist_event_type_ids",
    "hist_category": "hist_category_ids",
    "hist_brand": "hist_brand_ids",
    "hist_price_bucket": "hist_price_bucket_ids",
    "hist_time": "hist_time_ids",
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


def normalize_sequence_features(row: dict[str, Any]) -> dict[str, list[int]]:
    normalized = {}
    for triton_name, feature_name in HISTORY_FIELDS.items():
        normalized[triton_name] = [max(0, value) for value in as_int_list(row.get(feature_name))]
    return normalized


def normalize_item_features(item_id: int, row: dict[str, Any] | None) -> ItemFeatures:
    row = row or {}
    return ItemFeatures(
        item_id=int(item_id),
        category=max(0, int(row.get("category_id", 0) or 0)),
        brand=max(0, int(row.get("brand_id", 0) or 0)),
        price_bucket=max(0, int(row.get("price_bucket", 0) or 0)),
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
    def __init__(self) -> None:
        import redis

        self.client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
        )

    def user_sequence(self, user_id: int) -> dict[str, Any]:
        try:
            return parse_json_bytes(self.client.get(f"fs:user_sequence:{user_id}"))
        except Exception:
            return {}

    def item_features(self, item_id: int) -> dict[str, Any]:
        try:
            return parse_json_bytes(self.client.get(f"fs:item:{item_id}"))
        except Exception:
            return {}

    def candidates(self, user_id: int, limit: int) -> list[int]:
        try:
            raw = self.client.zrevrange("candidate:popular:global", 0, max(limit - 1, 0))
            return [int(item.decode("utf-8") if isinstance(item, bytes) else item) for item in raw]
        except Exception:
            return list(range(1, limit + 1))


class TritonRanker:
    def __init__(self) -> None:
        import tritonclient.grpc as grpcclient

        self.grpcclient = grpcclient
        self.client = grpcclient.InferenceServerClient(url=os.getenv("TRITON_URL", "localhost:8001"))
        self.model_name = os.getenv("TRITON_MODEL_NAME", "bst_ensemble")

    def score(self, payload: dict[str, np.ndarray]) -> tuple[list[int], list[float]]:
        inputs = []
        for name, values in payload.items():
            infer_input = self.grpcclient.InferInput(name, values.shape, "INT64")
            infer_input.set_data_from_numpy(values)
            inputs.append(infer_input)
        outputs = [
            self.grpcclient.InferRequestedOutput("candidate_item_id_out"),
            self.grpcclient.InferRequestedOutput("score"),
        ]
        result = self.client.infer(model_name=self.model_name, inputs=inputs, outputs=outputs)
        item_ids = result.as_numpy("candidate_item_id_out").astype(np.int64).reshape(-1).tolist()
        scores = result.as_numpy("score").astype(np.float32).reshape(-1).tolist()
        return item_ids, scores


def recommend(
    request: RecommendationRequest,
    feature_client: FeatureClient,
    ranker: TritonRanker,
    model_version: str,
) -> RecommendationResponse:
    candidate_item_ids = request.candidate_item_ids or feature_client.candidates(
        request.user_id,
        max(request.top_k * 5, request.top_k),
    )
    if not candidate_item_ids:
        return RecommendationResponse(user_id=request.user_id, model_version=model_version, items=[])
    sequence_row = feature_client.user_sequence(request.user_id)
    item_rows = {item_id: feature_client.item_features(item_id) for item_id in candidate_item_ids}
    payload = build_triton_payload(sequence_row, item_rows, candidate_item_ids)
    scored_item_ids, scores = ranker.score(payload)
    return format_top_k(
        user_id=request.user_id,
        model_version=model_version,
        candidate_item_ids=scored_item_ids,
        scores=scores,
        top_k=request.top_k,
    )
