from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from pydantic import ValidationError

import recsys_online_feature_api.service as service_module
from recsys_online_feature_api.service import (
    FeatureClient,
    _resolve,
    first_feature_row,
    get_online_features,
    normalize_feature_value,
    normalize_realtime_user_features,
    parse_json_bytes,
)
from recsys_serving_common.contracts import (
    OnlineFeaturesRequest,
    OnlineFeaturesResponse,
)


def test_shared_request_and_response_contracts() -> None:
    assert OnlineFeaturesRequest(user_id=1).model_dump() == {
        "user_id": 1,
        "candidate_item_ids": None,
        "top_k": 10,
    }
    assert (
        OnlineFeaturesRequest(user_id=1, candidate_item_ids=[1] * 500, top_k=100).top_k
        == 100
    )
    for payload in (
        {"user_id": 0},
        {"user_id": 1, "top_k": 0},
        {"user_id": 1, "top_k": 101},
        {"user_id": 1, "candidate_item_ids": []},
        {"user_id": 1, "candidate_item_ids": [1] * 501},
    ):
        with pytest.raises(ValidationError):
            OnlineFeaturesRequest.model_validate(payload)

    response = OnlineFeaturesResponse(
        user_id=7,
        candidate_item_ids=[1],
        user_sequence={"hist_item_ids": [2]},
        item_features={"1": {"brand_id": 3}},
    )
    assert response.model_dump()["item_features"] == {"1": {"brand_id": 3}}


def test_json_and_feature_normalization_branches() -> None:
    assert parse_json_bytes(None) == {}
    assert parse_json_bytes(b"") == {}
    assert parse_json_bytes(b'{"a": 1}') == {"a": 1}
    assert parse_json_bytes('{"b": 2}') == {"b": 2}

    array_like = SimpleNamespace(tolist=lambda: [1, 2])
    assert normalize_feature_value(array_like) == [1, 2]
    assert normalize_feature_value((3, 4)) == [3, 4]
    assert normalize_feature_value(5) == 5
    assert first_feature_row(
        {
            "user_id": [7],
            "empty": [],
            "missing": [None],
            "history": [(1, 2)],
            "score": [3],
        },
        {"user_id"},
    ) == {"history": [1, 2], "score": 3}

    normalized = normalize_realtime_user_features(
        {
            "item_ids": [1, 2],
            "event_type_ids": (3, 4),
            "category_ids": None,
            "sequence_length": 2,
            "ignored": "value",
        },
        {"views_30m": 5, "carts_30m": None, "ignored": 9},
    )
    assert normalized == {
        "hist_item_ids": [1, 2],
        "hist_event_type_ids": [3, 4],
        "hist_length": 2,
        "views_30m": 5,
    }


class BatchFeatureClient:
    def candidates(self, user_id: int, limit: int) -> list[int]:
        raise NotImplementedError

    def user_sequence(self, user_id: int) -> dict:
        raise NotImplementedError

    def item_features_batch(self, item_ids: list[int]) -> dict:
        raise NotImplementedError


def test_get_online_features_fallback_and_explicit_candidates() -> None:
    client = Mock(spec=BatchFeatureClient)
    client.candidates.return_value = [10, 11, 12]
    client.user_sequence.return_value = {"hist_item_ids": [7, 8]}
    client.item_features_batch.return_value = {
        "10": {"category_id": 1},
        "11": {"category_id": 2},
        "12": {"category_id": 3},
    }
    fallback = asyncio.run(get_online_features(7, None, 2, client))
    assert fallback.model_dump() == {
        "user_id": 7,
        "candidate_item_ids": [10, 11, 12],
        "user_sequence": {"hist_item_ids": [7, 8]},
        "item_features": {
            "10": {"category_id": 1},
            "11": {"category_id": 2},
            "12": {"category_id": 3},
        },
    }
    client.candidates.assert_called_once_with(7, 10)
    client.item_features_batch.assert_called_once_with([10, 11, 12])
    client.user_sequence.assert_called_once_with(7)

    client.reset_mock()
    client.item_features_batch = AsyncMock(
        return_value={"90": {"brand_id": 9}, "91": {"brand_id": 10}}
    )
    client.user_sequence = AsyncMock(return_value={"hist_item_ids": [1]})
    explicit = asyncio.run(get_online_features(3, [90, 91], 1, client))
    assert explicit.model_dump() == {
        "user_id": 3,
        "candidate_item_ids": [90, 91],
        "user_sequence": {"hist_item_ids": [1]},
        "item_features": {"90": {"brand_id": 9}, "91": {"brand_id": 10}},
    }
    client.candidates.assert_not_called()
    client.item_features_batch.assert_awaited_once_with([90, 91])
    client.user_sequence.assert_awaited_once_with(3)


class LegacyFeatureClient:
    def candidates(self, user_id: int, limit: int) -> list[int]:
        return [1, 2]

    async def user_sequence(self, user_id: int) -> dict:
        return {"hist_item_ids": [user_id]}

    async def item_features(self, item_id: int) -> dict:
        return {"category_id": item_id + 10}


def test_get_online_features_legacy_client_preserves_order() -> None:
    response = asyncio.run(get_online_features(5, None, 1, LegacyFeatureClient()))
    assert response.model_dump() == {
        "user_id": 5,
        "candidate_item_ids": [1, 2],
        "user_sequence": {"hist_item_ids": [5]},
        "item_features": {
            "1": {"category_id": 11},
            "2": {"category_id": 12},
        },
    }


def test_resolve_sync_and_async_values() -> None:
    async def value():
        return 7

    assert asyncio.run(_resolve(5)) == 5
    assert asyncio.run(_resolve(value())) == 7


def test_feature_client_batch_row_conversion_and_close() -> None:
    client = FeatureClient.__new__(FeatureClient)
    client._get_feast_online_features = Mock(
        return_value={
            "product_id": [10, 11],
            "category_id": [1, 2],
            "brand_id": [(3, 4)],
            "missing": [None, 9],
        }
    )
    rows = client._item_features_batch_sync([10, 11])
    assert rows == {
        "10": {"category_id": 1, "brand_id": [3, 4]},
        "11": {"category_id": 2, "missing": 9},
    }

    client._feast_executor = Mock()
    client._feast_executor.aclose = AsyncMock()
    client.client = Mock()
    client.client.aclose = AsyncMock()
    asyncio.run(client.aclose())
    client._feast_executor.aclose.assert_awaited_once()
    client.client.aclose.assert_awaited_once()


class FakeRedis:
    def __init__(self, personalized, global_candidates) -> None:
        self.personalized = personalized
        self.global_candidates = global_candidates
        self.calls = []

    async def zrevrange(self, key, start, stop):
        self.calls.append((key, start, stop))
        if key.startswith("candidate:user"):
            return self.personalized
        return self.global_candidates


def candidate_client(personalized, global_candidates, allow_fallback=False):
    client = FeatureClient.__new__(FeatureClient)
    client.client = FakeRedis(personalized, global_candidates)
    client.allow_fallback = allow_fallback
    return client


def test_candidate_generation_personalized_global_dedup_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter([2.0, 5.0] * 4 + [2.0, 5.0, 8.0])
    monkeypatch.setattr(service_module.time, "perf_counter", lambda: next(clock))
    observe = Mock()
    monkeypatch.setattr(service_module, "observe_redis", observe)
    metrics = Mock()
    monkeypatch.setattr(service_module, "METRICS", metrics)

    personalized = candidate_client([b"3", b"3", "4"], [b"9"])
    assert asyncio.run(personalized.candidates(7, 2)) == [3, 4]
    assert personalized.client.calls == [("candidate:user:7", 0, 1)]

    global_only = candidate_client([], [b"5", "6", b"5"])
    assert asyncio.run(global_only.candidates(7, 3)) == [5, 6]
    assert global_only.client.calls == [
        ("candidate:user:7", 0, 2),
        ("candidate:popular:global", 0, 2),
    ]

    fallback = candidate_client([], [], allow_fallback=True)
    assert asyncio.run(fallback.candidates(7, 3)) == []

    class BrokenRedis:
        async def zrevrange(self, *args):
            raise ConnectionError("redis down")

    fallback.client = BrokenRedis()
    assert asyncio.run(fallback.candidates(7, 3)) == [1, 2, 3]

    strict = candidate_client([], [], allow_fallback=False)
    with pytest.raises(RuntimeError, match="failed to fetch candidate"):
        asyncio.run(strict.candidates(7, 2))

    assert observe.call_args_list == [
        call("candidates", 3.0),
        call("candidates", 3.0),
        call("candidates", 3.0),
        call("candidates", 3.0, error=True),
        call("candidates", 3.0),
        call("candidates", 6.0, error=True),
    ]
    metrics.inc.assert_not_called()
