from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

import feature_api
import feature_service_client
import inference_api
from api_schemas import OnlineFeaturesRequest, OnlineFeaturesResponse, RecommendationRequest
from ranking import recommend
from serving_utils import bool_env, int_env


class DeterministicFeatureClient:
    def candidates(self, user_id: int, limit: int) -> list[int]:
        return list(range(100, 100 + limit))

    def user_sequence(self, user_id: int) -> dict[str, list[int]]:
        return {
            "hist_item_ids": [1, 2, 3],
            "hist_event_type_ids": [1, 1, 2],
            "hist_category_ids": [4, 5, 6],
            "hist_brand_ids": [7, 8, 9],
            "hist_price_bucket_ids": [1, 2, 3],
            "hist_time_ids": [1, 2, 3],
        }

    def item_features(self, item_id: int) -> dict[str, int]:
        return {
            "category_id": item_id % 30,
            "brand_id": item_id % 740,
            "price_bucket": item_id % 10,
        }


class DeterministicRanker:
    model_version = "deterministic-test"

    def score(self, payload):
        candidate_count = len(payload["candidate_item_id"])
        return payload["candidate_item_id"].tolist(), [float(index) for index in range(candidate_count)]


class DeterministicFeatureService:
    async def fetch(self, request: OnlineFeaturesRequest) -> OnlineFeaturesResponse:
        feature_client = DeterministicFeatureClient()
        candidates = request.candidate_item_ids or feature_client.candidates(request.user_id, request.top_k)
        return OnlineFeaturesResponse(
            user_id=request.user_id,
            candidate_item_ids=candidates,
            user_sequence=feature_client.user_sequence(request.user_id),
            item_features={str(item_id): feature_client.item_features(item_id) for item_id in candidates},
        )


@pytest.fixture
def deterministic_api(monkeypatch) -> TestClient:
    feature_service_impl = DeterministicFeatureService()
    feature_service = Mock(spec=DeterministicFeatureService)
    feature_service.fetch = AsyncMock(side_effect=feature_service_impl.fetch)
    ranker_impl = DeterministicRanker()
    ranker = Mock(spec=DeterministicRanker, wraps=ranker_impl)
    ranker.model_version = ranker_impl.model_version
    monkeypatch.setattr(inference_api, "feature_service_client", Mock(return_value=feature_service))
    monkeypatch.setattr(inference_api, "ranker", lambda: ranker)
    monkeypatch.setenv("MODEL_VERSION", ranker.model_version)
    client = TestClient(inference_api.app)
    client.app.state.feature_service_mock = feature_service
    client.app.state.ranker_mock = ranker
    return client


@pytest.fixture
def deterministic_feature_api(monkeypatch) -> TestClient:
    feature_client = Mock(spec=DeterministicFeatureClient, wraps=DeterministicFeatureClient())
    monkeypatch.setattr(feature_api, "feature_client", Mock(return_value=feature_client))
    return TestClient(feature_api.app)


@pytest.mark.parametrize(
    ("payload", "expected_count"),
    [
        ({"user_id": 42, "candidate_item_ids": [101, 102, 103], "top_k": 2}, 2),
        ({"user_id": 42, "top_k": 3}, 3),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 1}, 1),
        ({"user_id": 1, "candidate_item_ids": list(range(1, 501)), "top_k": 100}, 100),
    ],
    ids=[
        "equivalence-valid-explicit-candidates",
        "equivalence-valid-fallback-candidates",
        "boundary-min-user-top-k-and-one-candidate",
        "boundary-max-top-k-and-max-candidates",
    ],
)
def test_recommendations_web_api_equivalence_and_boundary_valid_cases(
    deterministic_api: TestClient,
    payload: dict,
    expected_count: int,
) -> None:
    response = deterministic_api.post("/recommendations", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["model_version"] == "deterministic-test"
    assert len(body["items"]) == expected_count
    deterministic_api.app.state.feature_service_mock.fetch.assert_awaited_once()
    deterministic_api.app.state.ranker_mock.score.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": 0, "candidate_item_ids": [1], "top_k": 1},
        {"user_id": 1, "candidate_item_ids": [1], "top_k": 0},
        {"user_id": 1, "candidate_item_ids": [1], "top_k": 101},
        {"user_id": 1, "candidate_item_ids": [], "top_k": 1},
        {"user_id": 1, "candidate_item_ids": list(range(1, 502)), "top_k": 1},
    ],
    ids=[
        "boundary-invalid-user-id-zero",
        "boundary-invalid-top-k-zero",
        "boundary-invalid-top-k-above-max",
        "boundary-invalid-empty-candidates",
        "boundary-invalid-candidates-above-max",
    ],
)
def test_recommendations_web_api_equivalence_and_boundary_invalid_cases(
    deterministic_api: TestClient,
    payload: dict,
) -> None:
    response = deterministic_api.post("/recommendations", json=payload)

    assert response.status_code == 422
    deterministic_api.app.state.feature_service_mock.fetch.assert_not_awaited()
    deterministic_api.app.state.ranker_mock.score.assert_not_called()


def test_api_health_ready_version_metrics_and_online_features(
    deterministic_api: TestClient,
    deterministic_feature_api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_VERSION", "api-surface-test")

    assert deterministic_api.get("/healthz").json() == {"status": "ok"}
    assert deterministic_api.get("/ready").json() == {"status": "ready"}
    version = deterministic_api.get("/version").json()
    assert version["service"] == "recsys-api-serving"
    assert version["model_version"] == "api-surface-test"
    assert version["inference_engine"] == "Triton Inference Server"

    metrics = deterministic_api.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers["content-type"]
    assert "recsys_observability_build_info" in metrics.text

    online = deterministic_feature_api.get(
        "/online-features/42",
        params=[("candidate_item_ids", 101), ("candidate_item_ids", 102), ("top_k", 2)],
    )
    assert online.status_code == 200
    body = online.json()
    assert body["user_id"] == 42
    assert body["candidate_item_ids"] == [101, 102]
    assert body["user_sequence"]["hist_item_ids"] == [1, 2, 3]
    assert body["item_features"]["101"]["category_id"] == 11

    feature_version = deterministic_feature_api.get("/version").json()
    assert feature_version["service"] == "recsys-online-feature-api"
    assert feature_version["online_store"] == "Redis"

    assert deterministic_feature_api.get("/ready").json() == {"status": "ready"}
    feature_metrics = deterministic_feature_api.get("/metrics")
    assert feature_metrics.status_code == 200
    assert "text/plain" in feature_metrics.headers["content-type"]
    assert "recsys_observability_build_info" in feature_metrics.text


def test_ready_can_be_forced_not_ready(
    deterministic_api: TestClient,
    deterministic_feature_api: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORCE_NOT_READY", "1")

    response = deterministic_api.get("/ready")
    feature_response = deterministic_feature_api.get("/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "forced not ready"
    assert feature_response.status_code == 503
    assert feature_response.json()["detail"] == "forced not ready"


def test_api_error_paths_return_bad_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenFeatureClient:
        def candidates(self, user_id: int, limit: int) -> list[int]:
            raise RuntimeError("feature store down")

        def user_sequence(self, user_id: int) -> dict:
            raise RuntimeError("feature store down")

        def item_features(self, item_id: int) -> dict:
            raise RuntimeError("feature store down")

    class BrokenRanker:
        def score(self, payload):
            raise RuntimeError("triton down")

    monkeypatch.setattr(feature_api, "feature_client", lambda: BrokenFeatureClient())
    feature_client = TestClient(feature_api.app)

    online = feature_client.get("/online-features/42", params={"top_k": 2})
    assert online.status_code == 502
    assert "online feature fetch failed" in online.json()["detail"]

    class BrokenFeatureService:
        async def fetch(self, request):
            raise RuntimeError("feature api down")

    monkeypatch.setattr(inference_api, "feature_service_client", lambda: BrokenFeatureService())
    monkeypatch.setattr(inference_api, "ranker", lambda: DeterministicRanker())
    client = TestClient(inference_api.app)

    recommendations = client.post("/recommendations", json={"user_id": 42, "top_k": 1})
    assert recommendations.status_code == 502
    assert "inference failed" in recommendations.json()["detail"]

    monkeypatch.setattr(inference_api, "feature_service_client", lambda: DeterministicFeatureService())
    monkeypatch.setattr(inference_api, "ranker", lambda: BrokenRanker())

    triton_failure = client.post("/recommendations", json={"user_id": 42, "top_k": 1})
    assert triton_failure.status_code == 502
    assert "inference failed" in triton_failure.json()["detail"]


def test_api_singletons_and_env_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    created_features = []
    created_feature_services = []
    created_rankers = []

    class FakeFeatureClient:
        def __init__(self) -> None:
            created_features.append("feature-client")

    class FakeFeatureServiceClient:
        def __init__(self) -> None:
            created_feature_services.append("feature-service-client")

    class FakeRouter:
        @classmethod
        def from_env(cls):
            created_rankers.append("ranker")
            return cls()

    monkeypatch.setattr(feature_api, "FeatureClient", FakeFeatureClient)
    monkeypatch.setattr(inference_api, "OnlineFeatureServiceClient", FakeFeatureServiceClient)
    monkeypatch.setattr(inference_api, "TritonABRouter", FakeRouter)
    monkeypatch.setattr(feature_api, "_feature_client", None)
    monkeypatch.setattr(inference_api, "_feature_service_client", None)
    monkeypatch.setattr(inference_api, "_ranker", None)
    monkeypatch.setenv("FEATURE_FLAG", "yes")
    monkeypatch.setenv("BAD_INT", "not-an-int")

    assert feature_api.feature_client() is feature_api.feature_client()
    assert inference_api.feature_service_client() is inference_api.feature_service_client()
    assert inference_api.ranker() is inference_api.ranker()
    assert created_features == ["feature-client"]
    assert created_feature_services == ["feature-service-client"]
    assert created_rankers == ["ranker"]
    assert bool_env("FEATURE_FLAG") is True
    assert bool_env("MISSING_FLAG", default="off") is False
    assert int_env("BAD_INT", default=7) == 7


def test_feature_api_startup_warmup_respects_env_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    warmups = []

    class WarmupFeatureClient:
        def _feature_store(self) -> str:
            warmups.append("warmed")
            return "store"

    monkeypatch.setattr(feature_api, "feature_client", lambda: WarmupFeatureClient())
    monkeypatch.setenv("FEATURE_API_WARMUP_ON_STARTUP", "1")
    asyncio.run(feature_api.warm_feature_store())
    assert warmups == ["warmed"]

    monkeypatch.setenv("FEATURE_API_WARMUP_ON_STARTUP", "0")
    asyncio.run(feature_api.warm_feature_store())
    assert warmups == ["warmed"]


def test_online_feature_service_client_fetches_and_validates_response(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            calls.append(("raise_for_status",))

        def json(self) -> dict:
            return {
                "user_id": 7,
                "candidate_item_ids": [101, 102],
                "user_sequence": {"hist_item_ids": [1, 2]},
                "item_features": {
                    "101": {"category_id": 11},
                    "102": {"category_id": 12},
                },
            }

    class FakeAsyncClient:
        def __init__(self, timeout: float) -> None:
            calls.append(("timeout", timeout))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            calls.append(("closed",))

        async def post(self, url: str, json: dict) -> FakeResponse:
            calls.append(("post", url, json))
            return FakeResponse()

    monkeypatch.setattr(feature_service_client.httpx, "AsyncClient", FakeAsyncClient)
    client = feature_service_client.OnlineFeatureServiceClient(
        base_url="http://feature-api/",
        timeout_seconds=1.5,
    )

    response = asyncio.run(
        client.fetch(
            OnlineFeaturesRequest(
                user_id=7,
                candidate_item_ids=[101, 102],
                top_k=2,
            )
        )
    )

    assert response.user_id == 7
    assert response.candidate_item_ids == [101, 102]
    assert response.item_features["102"]["category_id"] == 12
    assert ("timeout", 1.5) in calls
    assert ("raise_for_status",) in calls
    assert (
        "post",
        "http://feature-api/online-features",
        {"user_id": 7, "candidate_item_ids": [101, 102], "top_k": 2},
    ) in calls


@given(
    user_id=st.integers(min_value=1, max_value=20_000),
    top_k=st.integers(min_value=1, max_value=100),
    candidate_item_ids=st.lists(
        st.integers(min_value=1, max_value=20_000),
        min_size=1,
        max_size=100,
    ),
)
@settings(max_examples=60, deadline=None)
def test_property_based_recommendation_idempotency_for_deterministic_prediction(
    user_id: int,
    top_k: int,
    candidate_item_ids: list[int],
) -> None:
    request = RecommendationRequest(
        user_id=user_id,
        candidate_item_ids=candidate_item_ids,
        top_k=top_k,
    )
    feature_client = DeterministicFeatureClient()
    ranker = DeterministicRanker()

    responses = [
        recommend(
            request=request,
            feature_client=feature_client,
            ranker=ranker,
            model_version=ranker.model_version,
        ).model_dump()
        for _ in range(3)
    ]

    assert responses[0] == responses[1] == responses[2]
