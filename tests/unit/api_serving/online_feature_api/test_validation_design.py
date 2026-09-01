from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from recsys_online_feature_api.app import create_app
from recsys_online_feature_api.service import (
    FeatureClient,
    first_feature_row,
    get_online_features,
    normalize_feature_value,
)
from recsys_online_feature_api.settings import FeatureApiSettings


def get_params(
    user_id: int,
    top_k: int | None = None,
    candidate_item_ids: list[int] | None = None,
) -> tuple[str, list[tuple[str, int]]]:
    params: list[tuple[str, int]] = []
    if candidate_item_ids is not None:
        params.extend(("candidate_item_ids", item_id) for item_id in candidate_item_ids)
    if top_k is not None:
        params.append(("top_k", top_k))
    return f"/online-features/{user_id}", params


@pytest.mark.parametrize(
    ("payload", "expected_candidates", "uses_fallback"),
    [
        (
            {"user_id": 42, "candidate_item_ids": [101, 102, 103], "top_k": 2},
            [101, 102, 103],
            False,
        ),
        ({"user_id": 42, "top_k": 3}, list(range(100, 115)), True),
        (
            {"user_id": 42, "candidate_item_ids": list(range(1, 16))},
            list(range(1, 16)),
            False,
        ),
    ],
    ids=["ep-explicit-candidates", "ep-fallback-candidates", "ep-default-top-k"],
)
def test_post_equivalence_partitions(
    online_feature_api: TestClient,
    payload: dict,
    expected_candidates: list[int],
    uses_fallback: bool,
) -> None:
    response = online_feature_api.post("/online-features", json=payload)

    assert response.status_code == 200
    assert response.json()["candidate_item_ids"] == expected_candidates
    feature_client = online_feature_api.app.state.feature_client_mock
    feature_client.user_sequence.assert_called_once_with(payload["user_id"])
    feature_client.item_features_batch.assert_called_once_with(expected_candidates)
    if uses_fallback:
        expected_top_k = payload.get("top_k", 10)
        feature_client.candidates.assert_called_once_with(
            payload["user_id"], max(expected_top_k * 5, expected_top_k)
        )
    else:
        feature_client.candidates.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [None, {}, {"user_id": "not-an-int"}, ["not", "an", "object"]],
    ids=["no-body", "missing-user", "wrong-user-type", "array-body"],
)
def test_post_malformed_partition_does_not_call_dependencies(
    online_feature_api: TestClient, body: object
) -> None:
    response = online_feature_api.post("/online-features", json=body)

    assert response.status_code == 422
    feature_client = online_feature_api.app.state.feature_client_mock
    feature_client.candidates.assert_not_called()
    feature_client.user_sequence.assert_not_called()
    feature_client.item_features_batch.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"user_id": 0, "candidate_item_ids": [1], "top_k": 1}, 422),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 1}, 200),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 0}, 422),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 1}, 200),
        (
            {"user_id": 1, "candidate_item_ids": list(range(1, 501)), "top_k": 100},
            200,
        ),
        ({"user_id": 1, "candidate_item_ids": [1], "top_k": 101}, 422),
        ({"user_id": 1, "candidate_item_ids": [], "top_k": 1}, 422),
        ({"user_id": 1, "candidate_item_ids": list(range(1, 501)), "top_k": 1}, 200),
        ({"user_id": 1, "candidate_item_ids": list(range(1, 502)), "top_k": 1}, 422),
    ],
    ids=[
        "user-min-minus-one",
        "user-min",
        "top-k-min-minus-one",
        "top-k-min",
        "top-k-max",
        "top-k-max-plus-one",
        "candidate-count-min-minus-one",
        "candidate-count-max",
        "candidate-count-max-plus-one",
    ],
)
def test_post_boundary_value_analysis(
    online_feature_api: TestClient, payload: dict, expected_status: int
) -> None:
    response = online_feature_api.post("/online-features", json=payload)

    assert response.status_code == expected_status
    feature_client = online_feature_api.app.state.feature_client_mock
    if expected_status == 200:
        feature_client.user_sequence.assert_called_once()
        feature_client.item_features_batch.assert_called_once()
    else:
        feature_client.candidates.assert_not_called()
        feature_client.user_sequence.assert_not_called()
        feature_client.item_features_batch.assert_not_called()


@pytest.mark.parametrize(
    ("user_id", "top_k", "candidate_item_ids", "expected_status"),
    [
        (0, 1, [1], 422),
        (1, 1, [1], 200),
        (1, 0, [1], 422),
        (1, 1, [1], 200),
        (1, 100, [1], 200),
        (1, 101, [1], 422),
    ],
    ids=[
        "path-user-min-minus-one",
        "path-user-min",
        "query-top-k-min-minus-one",
        "query-top-k-min",
        "query-top-k-max",
        "query-top-k-max-plus-one",
    ],
)
def test_get_path_and_query_boundary_value_analysis(
    online_feature_api: TestClient,
    user_id: int,
    top_k: int,
    candidate_item_ids: list[int],
    expected_status: int,
) -> None:
    path, params = get_params(user_id, top_k, candidate_item_ids)
    response = online_feature_api.get(path, params=params)

    assert response.status_code == expected_status
    feature_client = online_feature_api.app.state.feature_client_mock
    if expected_status == 200:
        feature_client.user_sequence.assert_called_once()
        feature_client.item_features_batch.assert_called_once_with(candidate_item_ids)
    else:
        feature_client.candidates.assert_not_called()
        feature_client.user_sequence.assert_not_called()
        feature_client.item_features_batch.assert_not_called()


@pytest.mark.parametrize(
    ("candidate_count", "expected_status"),
    [(1, 200), (500, 200), (501, 422)],
    ids=[
        "query-candidate-count-min",
        "query-candidate-count-max",
        "query-candidate-count-max-plus-one",
    ],
)
def test_get_candidate_count_boundary_value_analysis(
    online_feature_api: TestClient,
    candidate_count: int,
    expected_status: int,
) -> None:
    candidates = list(range(1, candidate_count + 1))
    path, params = get_params(1, 1, candidates)
    response = online_feature_api.get(path, params=params)

    assert response.status_code == expected_status
    feature_client = online_feature_api.app.state.feature_client_mock
    if expected_status == 200:
        feature_client.item_features_batch.assert_called_once_with(candidates)
    else:
        feature_client.candidates.assert_not_called()
        feature_client.user_sequence.assert_not_called()
        feature_client.item_features_batch.assert_not_called()


def test_get_omitted_and_repeated_candidate_query_partitions(
    online_feature_api: TestClient,
) -> None:
    omitted = online_feature_api.get("/online-features/7", params={"top_k": 2})
    assert omitted.status_code == 200
    assert omitted.json()["candidate_item_ids"] == list(range(100, 110))

    feature_client = online_feature_api.app.state.feature_client_mock
    feature_client.reset_mock()
    repeated = online_feature_api.get(
        "/online-features/7",
        params=[
            ("candidate_item_ids", 91),
            ("candidate_item_ids", 92),
            ("top_k", 2),
        ],
    )
    assert repeated.status_code == 200
    assert repeated.json()["candidate_item_ids"] == [91, 92]
    feature_client.candidates.assert_not_called()
    feature_client.item_features_batch.assert_called_once_with([91, 92])


@pytest.mark.parametrize("candidate_item_ids", [None, [91, 92]])
def test_post_and_get_are_response_equivalent(
    online_feature_api: TestClient, candidate_item_ids: list[int] | None
) -> None:
    payload = {"user_id": 7, "top_k": 2}
    if candidate_item_ids is not None:
        payload["candidate_item_ids"] = candidate_item_ids
    post = online_feature_api.post("/online-features", json=payload)
    path, params = get_params(7, 2, candidate_item_ids)
    get = online_feature_api.get(path, params=params)

    assert post.status_code == get.status_code == 200
    assert post.json() == get.json()


@given(
    user_id=st.integers(min_value=1, max_value=20_000),
    top_k=st.integers(min_value=1, max_value=100),
    candidate_item_ids=st.one_of(
        st.none(),
        st.lists(st.integers(min_value=1, max_value=20_000), min_size=1, max_size=40),
    ),
)
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_post_http_idempotency(
    online_feature_api: TestClient,
    user_id: int,
    top_k: int,
    candidate_item_ids: list[int] | None,
) -> None:
    feature_client = online_feature_api.app.state.feature_client_mock
    feature_client.reset_mock()
    payload = {"user_id": user_id, "top_k": top_k}
    if candidate_item_ids is not None:
        payload["candidate_item_ids"] = candidate_item_ids

    responses = [
        online_feature_api.post("/online-features", json=payload) for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert feature_client.user_sequence.call_count == 3
    assert feature_client.item_features_batch.call_count == 3


@given(
    user_id=st.integers(min_value=1, max_value=20_000),
    top_k=st.integers(min_value=1, max_value=100),
    candidate_item_ids=st.one_of(
        st.none(),
        st.lists(st.integers(min_value=1, max_value=20_000), min_size=1, max_size=40),
    ),
)
@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_get_http_idempotency(
    online_feature_api: TestClient,
    user_id: int,
    top_k: int,
    candidate_item_ids: list[int] | None,
) -> None:
    feature_client = online_feature_api.app.state.feature_client_mock
    feature_client.reset_mock()
    path, params = get_params(user_id, top_k, candidate_item_ids)

    responses = [online_feature_api.get(path, params=params) for _ in range(3)]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert responses[0].json() == responses[1].json() == responses[2].json()
    assert feature_client.user_sequence.call_count == 3
    assert feature_client.item_features_batch.call_count == 3


def test_health_ready_version_metrics(online_feature_api: TestClient) -> None:
    assert online_feature_api.get("/healthz").json() == {"status": "ok"}
    assert online_feature_api.get("/ready").json() == {"status": "ready"}
    version = online_feature_api.get("/version").json()
    assert version["service"] == "recsys-online-feature-api"
    assert version["online_store"] == "Redis"
    metrics = online_feature_api.get("/metrics")
    assert metrics.status_code == 200
    assert "recsys_observability_build_info" in metrics.text


def test_ready_can_be_forced_not_ready(
    online_feature_api: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORCE_NOT_READY", "1")
    response = online_feature_api.get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "forced not ready"


def test_dependency_failure_returns_bad_gateway() -> None:
    class BrokenFeatureClient:
        def candidates(self, user_id: int, limit: int) -> list[int]:
            raise RuntimeError("feature store down")

        def close(self) -> None:
            pass

    with TestClient(
        create_app(
            FeatureApiSettings(warmup_on_startup=False),
            feature_client=BrokenFeatureClient(),
        )
    ) as client:
        response = client.get("/online-features/42", params={"top_k": 2})
        assert response.status_code == 502
        assert "online feature fetch failed" in response.json()["detail"]


def test_feature_service_collaboration_and_legacy_path() -> None:
    client = Mock()
    client.candidates.return_value = [10, 11, 12]
    client.user_sequence.return_value = {"hist_item_ids": [7]}
    client.item_features_batch.return_value = {
        "10": {"category_id": 10},
        "11": {"category_id": 11},
        "12": {"category_id": 12},
    }
    fallback = asyncio.run(get_online_features(7, None, 2, client))
    explicit = asyncio.run(get_online_features(7, [90, 91], 2, client))
    assert fallback.candidate_item_ids == [10, 11, 12]
    assert explicit.candidate_item_ids == [90, 91]
    client.candidates.assert_called_once_with(7, 10)

    class LegacyFeatureClient:
        def candidates(self, user_id: int, limit: int) -> list[int]:
            return [1, 2]

        async def user_sequence(self, user_id: int) -> dict:
            return {"hist_item_ids": [user_id]}

        async def item_features(self, item_id: int) -> dict:
            return {"category_id": item_id + 10}

    legacy = asyncio.run(get_online_features(5, None, 1, LegacyFeatureClient()))
    assert legacy.item_features == {
        "1": {"category_id": 11},
        "2": {"category_id": 12},
    }


def test_feature_client_close_and_normalization_helpers() -> None:
    client = FeatureClient.__new__(FeatureClient)
    client._feast_executor = Mock()
    client._feast_executor.aclose = AsyncMock()
    client.client = Mock()
    client.client.aclose = AsyncMock()
    asyncio.run(client.aclose())
    client._feast_executor.aclose.assert_awaited_once()
    client.client.aclose.assert_awaited_once()

    array_like = SimpleNamespace(tolist=lambda: [1, 2])
    assert normalize_feature_value(array_like) == [1, 2]
    assert normalize_feature_value((3, 4)) == [3, 4]
    assert first_feature_row(
        {"user_id": [7], "empty": [], "missing": [None], "history": [(1, 2)]},
        {"user_id"},
    ) == {"history": [1, 2]}


def test_feature_api_settings_and_startup_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FEATURE_CAPACITY_WAIT_SECONDS", raising=False)
    assert FeatureApiSettings.from_env().capacity_wait_seconds == 5.0
    monkeypatch.setenv("FEATURE_CAPACITY_WAIT_SECONDS", "5")
    assert FeatureApiSettings.from_env().capacity_wait_seconds == 5.0

    warmups: list[str] = []

    class WarmupFeatureClient:
        def _feature_store(self) -> str:
            warmups.append("warmed")
            return "store"

        def close(self) -> None:
            pass

    with TestClient(
        create_app(
            FeatureApiSettings(warmup_on_startup=True),
            feature_client=WarmupFeatureClient(),
        )
    ):
        pass
    assert warmups == ["warmed"]

    with TestClient(
        create_app(
            FeatureApiSettings(warmup_on_startup=False),
            feature_client=WarmupFeatureClient(),
        )
    ):
        pass
    assert warmups == ["warmed"]
