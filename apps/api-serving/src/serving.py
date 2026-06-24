from __future__ import annotations

import json
import os
import hashlib
import time
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np
from pydantic import BaseModel, Field

from observability import METRICS, observe_model_prediction, observe_redis, observe_triton, span


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
    ab_variant: str | None = None
    ab_experiment_id: str | None = None
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


@dataclass(frozen=True)
class TritonRoute:
    ranker: "RankerProtocol"
    model_version: str
    ab_variant: str | None = None
    ab_experiment_id: str | None = None


class RankerProtocol(Protocol):
    def score(self, payload: dict[str, np.ndarray]) -> tuple[list[int], list[float]]:
        ...


def _bool_env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _ab_labels(
    ab_variant: str | None,
    model_version: str,
    ab_experiment_id: str | None,
) -> dict[str, str]:
    return {
        "ab_variant": ab_variant or "none",
        "model_version": model_version,
        "experiment_id": ab_experiment_id or "none",
    }


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
    def __init__(
        self,
        url: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        ab_variant: str | None = None,
        ab_experiment_id: str | None = None,
    ) -> None:
        import tritonclient.grpc as grpcclient

        self.grpcclient = grpcclient
        self.client = grpcclient.InferenceServerClient(url=url or os.getenv("TRITON_URL", "localhost:8001"))
        self.model_name = model_name or os.getenv("TRITON_MODEL_NAME", "bst_ensemble")
        self.model_version = model_version or os.getenv("MODEL_VERSION", "latest")
        self.ab_variant = ab_variant
        self.ab_experiment_id = ab_experiment_id

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
            observe_triton(
                self.model_name,
                time.perf_counter() - start,
                labels=_ab_labels(self.ab_variant, self.model_version, self.ab_experiment_id),
            )
            return item_ids, scores
        except Exception:
            observe_triton(
                self.model_name,
                time.perf_counter() - start,
                error=True,
                labels=_ab_labels(self.ab_variant, self.model_version, self.ab_experiment_id),
            )
            raise


class TritonABRouter:
    def __init__(
        self,
        control_ranker: RankerProtocol,
        control_model_version: str,
        candidate_ranker: RankerProtocol | None = None,
        candidate_model_version: str | None = None,
        enabled: bool = False,
        candidate_weight_percent: int = 0,
        experiment_id: str = "",
    ) -> None:
        self.control_ranker = control_ranker
        self.control_model_version = control_model_version
        self.candidate_ranker = candidate_ranker
        self.candidate_model_version = candidate_model_version or control_model_version
        self.enabled = enabled and candidate_ranker is not None
        self.candidate_weight_percent = max(0, min(100, candidate_weight_percent))
        self.experiment_id = experiment_id or "default"

    @classmethod
    def from_env(cls) -> "TritonABRouter":
        model_name = os.getenv("TRITON_MODEL_NAME", "bst_ensemble")
        control_version = os.getenv("AB_CONTROL_MODEL_VERSION") or os.getenv("MODEL_VERSION", "latest")
        candidate_version = os.getenv("AB_CANDIDATE_MODEL_VERSION", "")
        experiment_id = os.getenv("AB_EXPERIMENT_ID", "default")
        control_ranker = TritonRanker(
            url=os.getenv("AB_CONTROL_TRITON_URL") or os.getenv("TRITON_URL", "localhost:8001"),
            model_name=model_name,
            model_version=control_version,
            ab_variant="control",
            ab_experiment_id=experiment_id,
        )
        candidate_ranker: TritonRanker | None = None
        if _bool_env("AB_TEST_ENABLED") and os.getenv("AB_CANDIDATE_TRITON_URL"):
            candidate_ranker = TritonRanker(
                url=os.getenv("AB_CANDIDATE_TRITON_URL"),
                model_name=model_name,
                model_version=candidate_version or control_version,
                ab_variant="candidate",
                ab_experiment_id=experiment_id,
            )
        return cls(
            control_ranker=control_ranker,
            control_model_version=control_version,
            candidate_ranker=candidate_ranker,
            candidate_model_version=candidate_version or control_version,
            enabled=_bool_env("AB_TEST_ENABLED"),
            candidate_weight_percent=_int_env("AB_CANDIDATE_WEIGHT_PERCENT"),
            experiment_id=experiment_id,
        )

    def assign(self, user_id: int) -> str:
        if not self.enabled or self.candidate_weight_percent <= 0:
            return "control"
        if self.candidate_weight_percent >= 100:
            return "candidate"
        key = f"{self.experiment_id}:{int(user_id)}".encode("utf-8")
        bucket = int(hashlib.sha256(key).hexdigest()[:8], 16) % 100
        return "candidate" if bucket < self.candidate_weight_percent else "control"

    def route(self, user_id: int) -> TritonRoute:
        variant = self.assign(user_id)
        if variant == "candidate" and self.candidate_ranker is not None:
            route = TritonRoute(
                ranker=self.candidate_ranker,
                model_version=self.candidate_model_version,
                ab_variant="candidate",
                ab_experiment_id=self.experiment_id,
            )
        else:
            route = TritonRoute(
                ranker=self.control_ranker,
                model_version=self.control_model_version,
                ab_variant="control" if self.enabled else None,
                ab_experiment_id=self.experiment_id if self.enabled else None,
            )
        METRICS.inc("recsys_api_ab_assignments_total", labels=_ab_labels(
            route.ab_variant,
            route.model_version,
            route.ab_experiment_id,
        ))
        return route


def select_triton_route(
    ranker: RankerProtocol | TritonABRouter,
    user_id: int,
    model_version: str,
) -> TritonRoute:
    if hasattr(ranker, "route"):
        return ranker.route(user_id)  # type: ignore[union-attr]
    return TritonRoute(ranker=ranker, model_version=model_version)


def recommend(
    request: RecommendationRequest,
    feature_client: FeatureClient,
    ranker: RankerProtocol | TritonABRouter,
    model_version: str,
) -> RecommendationResponse:
    route = select_triton_route(ranker, request.user_id, model_version)
    metric_labels = _ab_labels(route.ab_variant, route.model_version, route.ab_experiment_id)
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
    candidate_item_ids = online_features.candidate_item_ids
    METRICS.set_gauge("recsys_api_candidate_count", len(candidate_item_ids), labels=metric_labels)
    if not candidate_item_ids:
        METRICS.inc("recsys_api_empty_recommendations_total", labels=metric_labels)
        return RecommendationResponse(
            user_id=request.user_id,
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
    with span("recommend.format_top_k", top_k=request.top_k):
        response = format_top_k(
            user_id=request.user_id,
            model_version=route.model_version,
            candidate_item_ids=candidate_item_ids,
            scores=scores,
            top_k=request.top_k,
            ab_variant=route.ab_variant,
            ab_experiment_id=route.ab_experiment_id,
        )
    METRICS.set_gauge("recsys_api_recommendation_items_count", len(response.items), labels=metric_labels)
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
