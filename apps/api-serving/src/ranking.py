from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from ab_testing import TritonABRouter, TritonRoute, select_triton_route
from online_features import FeatureClient, get_online_features
from observability import METRICS, observe_model_prediction, span
from api_schemas import OnlineFeaturesResponse, RecommendationItem, RecommendationRequest, RecommendationResponse
from serving_utils import ab_labels
from triton import RankerProtocol


CARDINALITY_ENV = {
    "item": ("MODEL_ITEM_NUM", 22700),
    "category": ("MODEL_CATEGORY_NUM", 30),
    "brand": ("MODEL_BRAND_NUM", 740),
    "price_bucket": ("MODEL_PRICE_BUCKET_NUM", 10),
    "event_type": ("MODEL_EVENT_TYPE_NUM", 3),
    "time": ("MODEL_TIME_BUCKET_NUM", 9),
}


@dataclass(frozen=True)
class ItemFeatures:
    item_id: int
    category: int = 0
    brand: int = 0
    price_bucket: int = 0


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
    payload = {name: np.asarray(values, dtype=np.int64) for name, values in sequence.items()}
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
    ab_variant: str | None = None,
    ab_experiment_id: str | None = None,
) -> RecommendationResponse:
    pairs = sorted(
        zip(candidate_item_ids, [float(score) for score in scores]),
        key=lambda item: item[1],
        reverse=True,
    )[:top_k]
    return RecommendationResponse(
        user_id=user_id,
        model_version=model_version,
        ab_variant=ab_variant,
        ab_experiment_id=ab_experiment_id,
        items=[RecommendationItem(item_id=int(item_id), score=score) for item_id, score in pairs],
    )


def recommend(
    request: RecommendationRequest,
    feature_client: FeatureClient,
    ranker: RankerProtocol | TritonABRouter,
    model_version: str,
) -> RecommendationResponse:
    route = select_triton_route(ranker, request.user_id, model_version)
    metric_labels = ab_labels(route.ab_variant, route.model_version, route.ab_experiment_id)
    start = time.perf_counter()
    status = "error"
    confidence: float | None = None
    try:
        response = _recommend_with_route(request, feature_client, route, metric_labels)
        status = "success" if response.items else "empty"
        if response.items:
            confidence = max(item.score for item in response.items)
        return response
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


def _recommend_with_route(
    request: RecommendationRequest,
    feature_client: FeatureClient,
    route: TritonRoute,
    metric_labels: dict[str, str],
) -> RecommendationResponse:
    with span("recommend.get_online_features", top_k=request.top_k):
        online_features = get_online_features(
            user_id=request.user_id,
            candidate_item_ids=request.candidate_item_ids,
            top_k=request.top_k,
            feature_client=feature_client,
        )
    return recommend_from_online_features(
        online_features=online_features,
        top_k=request.top_k,
        route=route,
        metric_labels=metric_labels,
    )


def recommend_from_online_features(
    online_features: OnlineFeaturesResponse,
    top_k: int,
    route: TritonRoute,
    metric_labels: dict[str, str] | None = None,
) -> RecommendationResponse:
    metric_labels = metric_labels or {}
    candidate_item_ids = online_features.candidate_item_ids
    METRICS.set_gauge("recsys_api_candidate_count", len(candidate_item_ids), labels=metric_labels)
    if not candidate_item_ids:
        METRICS.inc("recsys_api_empty_recommendations_total", labels=metric_labels)
        return RecommendationResponse(
            user_id=online_features.user_id,
            model_version=route.model_version,
            ab_variant=route.ab_variant,
            ab_experiment_id=route.ab_experiment_id,
            items=[],
        )
    sequence_row = online_features.user_sequence
    item_rows = {int(item_id): row for item_id, row in online_features.item_features.items()}
    with span("recommend.build_triton_payload", candidate_count=len(candidate_item_ids)):
        payload = build_triton_payload(sequence_row, item_rows, candidate_item_ids)
    _, scores = route.ranker.score(payload)
    if scores:
        METRICS.observe("recsys_api_score_mean", float(np.mean(scores)), labels=metric_labels)
        METRICS.set_gauge("recsys_api_score_max", float(np.max(scores)), labels=metric_labels)
    with span("recommend.format_top_k", top_k=top_k):
        response = format_top_k(
            user_id=online_features.user_id,
            model_version=route.model_version,
            candidate_item_ids=candidate_item_ids,
            scores=scores,
            top_k=top_k,
            ab_variant=route.ab_variant,
            ab_experiment_id=route.ab_experiment_id,
        )
    METRICS.set_gauge("recsys_api_recommendation_items_count", len(response.items), labels=metric_labels)
    return response
