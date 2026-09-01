from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from recsys_inference_api import feature_client as feature_service_client
from recsys_inference_api.app import create_app
from recsys_inference_api.settings import InferenceApiSettings
from recsys_serving_common.contracts import OnlineFeaturesRequest
from recsys_serving_common.env import bool_env, int_env


@pytest.mark.parametrize(
    ("payload", "expected_count", "partition"),
    [
        (
            {"user_id": 42, "candidate_item_ids": [101, 102, 103], "top_k": 2},
            2,
            "explicit-candidates",
        ),
        ({"user_id": 42, "top_k": 3}, 3, "fallback-candidates"),
        (
            {"user_id": 42, "candidate_item_ids": list(range(1, 16))},
            10,
            "default-top-k",
        ),
    ],
    ids=[
        "ep-explicit-candidates",
        "ep-fallback-candidates",
        "ep-default-top-k",
    ],
)
def test_recommendations_equivalence_partitions(
    inference_api: TestClient,
    payload: dict,
    expected_count: int,
    partition: str,
) -> None:
    response = inference_api.post("/recommendations", json=payload)

    assert response.status_code == 200, partition
    assert response.json()["model_version"] == "deterministic-test"
    assert len(response.json()["items"]) == expected_count
    inference_api.app.state.feature_service_mock.fetch.assert_awaited_once()
    inference_api.app.state.ranker_mock.score.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [None, {}, {"user_id": "not-an-int"}, ["not", "an", "object"]],
    ids=["no-body", "missing-user", "wrong-user-type", "array-body"],
)
def test_recommendations_malformed_partition_does_not_call_dependencies(
    inference_api: TestClient,
    body: object,
) -> None:
    response = inference_api.post("/recommendations", json=body)

    assert response.status_code == 422
    inference_api.app.state.feature_service_mock.fetch.assert_not_awaited()
    inference_api.app.state.ranker_mock.score.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_count"),
    [
        ({"user_id": 0, "candidate_item_ids": [1], "top_k": 1}, 422, None),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 1}, 200, 1),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 0}, 422, None),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 1}, 200, 1),
        (
            {"user_id": 1, "candidate_item_ids": list(range(1, 501)), "top_k": 100},
            200,
            100,
        ),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 101}, 422, None),
        ({"user_id": 1, "candidate_item_ids": [], "top_k": 1}, 422, None),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 1}, 200, 1),
        (
            {"user_id": 1, "candidate_item_ids": list(range(1, 501)), "top_k": 1},
            200,
            1,
        ),
        (
            {"user_id": 1, "candidate_item_ids": list(range(1, 502)), "top_k": 1},
            422,
            None,
        ),
    ],
    ids=[
        "user-min-minus-one",
        "user-min",
        "top-k-min-minus-one",
        "top-k-min",
        "top-k-max",
        "top-k-max-plus-one",
        "candidate-count-min-minus-one",
        "candidate-count-min",
        "candidate-count-max",
        "candidate-count-max-plus-one",
    ],
)
def test_recommendations_boundary_value_analysis(
    inference_api: TestClient,
    payload: dict,
    expected_status: int,
    expected_count: int | None,
) -> None:
    response = inference_api.post("/recommendations", json=payload)

    assert response.status_code == expected_status
    feature_service = inference_api.app.state.feature_service_mock
    ranker = inference_api.app.state.ranker_mock
    if expected_status == 200:
        assert len(response.json()["items"]) == expected_count
        feature_service.fetch.assert_awaited_once()
        ranker.score.assert_called_once()
    else:
        feature_service.fetch.assert_not_awaited()
        ranker.score.assert_not_called()


@given(
    user_id=st.integers(min_value=1, max_value=20_000),
    top_k=st.integers(min_value=1, max_value=100),
    candidate_item_ids=st.one_of(
        st.none(),
        st.lists(
            st.integers(min_value=1, max_value=20_000),
            min_size=1,
            max_size=40,
        ),
    ),
)
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_recommendations_http_idempotency(
    inference_api: TestClient,
    user_id: int,
    top_k: int,
    candidate_item_ids: list[int] | None,
) -> None:
    feature_service = inference_api.app.state.feature_service_mock
    ranker = inference_api.app.state.ranker_mock
    feature_service.fetch.reset_mock()
    ranker.score.reset_mock()
    payload = {"user_id": user_id, "top_k": top_k}
    if candidate_item_ids is not None:
        payload["candidate_item_ids"] = candidate_item_ids

    responses = [inference_api.post("/recommendations", json=payload) for _ in range(3)]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert feature_service.fetch.await_count == 3
    assert ranker.score.call_count == 3


def test_health_ready_version_and_metrics(inference_api: TestClient) -> None:
    assert inference_api.get("/healthz").json() == {"status": "ok"}
    assert inference_api.get("/ready").json() == {"status": "ready"}
    version = inference_api.get("/version").json()
    assert version["service"] == "recsys-inference-api"
    assert version["model_version"] == "deterministic-test"
    assert version["inference_engine"] == "Triton Inference Server"
    metrics = inference_api.get("/metrics")
    assert metrics.status_code == 200
    assert "recsys_observability_build_info" in metrics.text


def test_ready_can_be_forced_not_ready(
    inference_api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_NOT_READY", "1")
    response = inference_api.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "forced not ready"


def test_recommendations_dependency_failures_return_bad_gateway() -> None:
    class BrokenFeatureService:
        async def fetch(self, request):
            raise RuntimeError("feature api down")

    class WorkingRanker:
        model_version = "test"

        async def score(self, payload):
            candidates = payload["candidate_item_id"].tolist()
            return candidates, [1.0 for _ in candidates]

    class BrokenRanker(WorkingRanker):
        async def score(self, payload):
            raise RuntimeError("triton down")

    class WorkingFeatureService:
        async def fetch(self, request):
            from recsys_serving_common.contracts import OnlineFeaturesResponse

            candidates = request.candidate_item_ids or [1]
            return OnlineFeaturesResponse(
                user_id=request.user_id,
                candidate_item_ids=candidates,
                user_sequence={},
                item_features={str(item_id): {} for item_id in candidates},
            )

    settings = InferenceApiSettings("http://feature-api", 1.0, "test", 1.0, 4, 1)
    with TestClient(
        create_app(
            settings,
            feature_service=BrokenFeatureService(),
            router=WorkingRanker(),
        )
    ) as client:
        response = client.post("/recommendations", json={"user_id": 42, "top_k": 1})
        assert response.status_code == 502
        assert "inference failed" in response.json()["detail"]

    with TestClient(
        create_app(
            settings,
            feature_service=WorkingFeatureService(),
            router=BrokenRanker(),
        )
    ) as client:
        response = client.post("/recommendations", json={"user_id": 42, "top_k": 1})
        assert response.status_code == 502
        assert "inference failed" in response.json()["detail"]


def test_online_feature_service_client_selects_post_or_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            calls.append(("raise_for_status",))

        def json(self) -> dict:
            return {
                "user_id": 7,
                "candidate_item_ids": [101, 102],
                "user_sequence": {"hist_item_ids": [1, 2]},
                "item_features": {"101": {}, "102": {}},
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

        async def get(self, url: str, params: dict) -> FakeResponse:
            calls.append(("get", url, params))
            return FakeResponse()

    monkeypatch.setattr(feature_service_client.httpx, "AsyncClient", FakeAsyncClient)
    client = feature_service_client.OnlineFeatureServiceClient(
        base_url="http://feature-api/", timeout_seconds=1.5
    )
    asyncio.run(
        client.fetch(
            OnlineFeaturesRequest(user_id=7, candidate_item_ids=[101, 102], top_k=2)
        )
    )
    asyncio.run(client.fetch(OnlineFeaturesRequest(user_id=7, top_k=2)))

    assert (
        "post",
        "http://feature-api/online-features",
        {"user_id": 7, "candidate_item_ids": [101, 102], "top_k": 2},
    ) in calls
    assert ("get", "http://feature-api/online-features/7", {"top_k": 2}) in calls


def test_typed_settings_and_environment_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRITON_CAPACITY_WAIT_SECONDS", raising=False)
    assert InferenceApiSettings.from_env().triton_capacity_wait_seconds == 5.0

    monkeypatch.setenv("TRITON_CAPACITY_WAIT_SECONDS", "5")
    monkeypatch.setenv("FEATURE_FLAG", "yes")
    monkeypatch.setenv("BAD_INT", "not-an-int")
    monkeypatch.setenv("MODEL_VERSION", "settings-v1")
    loaded = InferenceApiSettings.from_env()
    assert loaded.model_version == "settings-v1"
    assert loaded.triton_capacity_wait_seconds == 5.0
    assert bool_env("FEATURE_FLAG") is True
    assert bool_env("MISSING_FLAG", default="off") is False
    assert int_env("BAD_INT", default=7) == 7
