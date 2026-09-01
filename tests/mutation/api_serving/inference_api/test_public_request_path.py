from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
import hashlib
from unittest.mock import AsyncMock, Mock, call

import numpy as np
import pytest
from pydantic import ValidationError

import recsys_inference_api.ab_testing as ab_module
import recsys_inference_api.feature_client as feature_client_module
import recsys_inference_api.ranking as ranking_module
import recsys_inference_api.shadow as shadow_module
import recsys_inference_api.triton as triton_module
from recsys_inference_api.ab_testing import (
    TritonABRouter,
    TritonRoute,
    select_triton_route,
)
from recsys_inference_api.feature_client import OnlineFeatureServiceClient
from recsys_inference_api.ranking import (
    as_int_list,
    build_triton_payload,
    embedding_index,
    format_top_k,
    normalize_item_features,
    normalize_sequence_features,
    recommend_from_online_features,
)
from recsys_inference_api.schemas import RecommendationRequest
from recsys_inference_api.shadow import ShadowRunner
from recsys_inference_api.triton import TritonRanker
from recsys_serving_common.contracts import (
    OnlineFeaturesRequest,
    OnlineFeaturesResponse,
)


def test_request_contract_boundaries() -> None:
    assert RecommendationRequest(user_id=1).top_k == 10
    assert (
        RecommendationRequest(
            user_id=1, candidate_item_ids=[1] * 500, top_k=100
        ).user_id
        == 1
    )
    for payload in (
        {"user_id": 0},
        {"user_id": 1, "top_k": 0},
        {"user_id": 1, "top_k": 101},
        {"user_id": 1, "candidate_item_ids": []},
        {"user_id": 1, "candidate_item_ids": [1] * 501},
    ):
        with pytest.raises(ValidationError):
            RecommendationRequest.model_validate(payload)


def test_ranking_normalization_payload_and_top_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert as_int_list(None) == []
    assert as_int_list("[1, null, 2]") == [1, 2]
    assert as_int_list("invalid") == []
    assert as_int_list((3, 4)) == [3, 4]
    assert as_int_list({"bad": "shape"}) == []

    monkeypatch.setenv("MODEL_ITEM_NUM", "10")
    assert embedding_index(-1, "item") == 0
    assert embedding_index(12, "item") == 2
    sequence = normalize_sequence_features(
        {
            "hist_item_ids": [1, 12],
            "hist_event_type_ids": [2],
            "hist_category_ids": [3],
            "hist_brand_ids": [4],
            "hist_price_bucket_ids": [5],
            "hist_time_ids": [6],
        }
    )
    assert sequence == {
        "hist_item_id": [1, 2],
        "hist_event_type": [2],
        "hist_category": [3],
        "hist_brand": [4],
        "hist_price_bucket": [5],
        "hist_time": [6],
    }
    item = normalize_item_features(12, {"category_id": 31, "brand_id": 741})
    assert (item.item_id, item.category, item.brand, item.price_bucket) == (2, 1, 1, 0)

    payload = build_triton_payload(
        {"hist_item_ids": [1]},
        {12: {"category_id": 2, "brand_id": 3, "price_bucket": 4}},
        [12],
    )
    assert set(payload) == {
        "hist_item_id",
        "hist_event_type",
        "hist_category",
        "hist_brand",
        "hist_price_bucket",
        "hist_time",
        "candidate_item_id",
        "candidate_category",
        "candidate_brand",
        "candidate_price_bucket",
    }
    assert payload["candidate_item_id"].tolist() == [2]
    assert payload["candidate_category"].tolist() == [2]

    response = format_top_k(
        7,
        "model-v2",
        [10, 11, 12, 13],
        [0.2, 0.9, 0.5, -0.1],
        2,
        "candidate",
        "experiment-1",
    )
    assert response.model_dump() == {
        "user_id": 7,
        "model_version": "model-v2",
        "ab_variant": "candidate",
        "ab_experiment_id": "experiment-1",
        "items": [
            {"item_id": 11, "score": 0.9},
            {"item_id": 12, "score": 0.5},
        ],
    }
    assert format_top_k(1, "v", [1], [1.0], 0).items == []


class Ranker:
    def __init__(self, scores: list[float]) -> None:
        self.scores = scores
        self.calls = []

    async def score(self, payload):
        self.calls.append(payload)
        return payload["candidate_item_id"].tolist(), self.scores


def test_recommendation_business_path_and_empty_short_circuit() -> None:
    observed = []
    ranker = Ranker([0.1, 0.8])
    route = TritonRoute(ranker, "v2", "candidate", "exp")
    features = OnlineFeaturesResponse(
        user_id=7,
        candidate_item_ids=[10, 11],
        user_sequence={"hist_item_ids": [1]},
        item_features={"10": {"category_id": 1}, "11": {"category_id": 2}},
    )
    result = asyncio.run(
        recommend_from_online_features(
            features,
            1,
            route,
            metric_labels={"test": "value"},
            payload_observer=observed.append,
        )
    )
    assert result.model_dump() == {
        "user_id": 7,
        "model_version": "v2",
        "ab_variant": "candidate",
        "ab_experiment_id": "exp",
        "items": [{"item_id": 11, "score": 0.8}],
    }
    assert len(ranker.calls) == len(observed) == 1

    empty = asyncio.run(
        recommend_from_online_features(
            OnlineFeaturesResponse(
                user_id=8,
                candidate_item_ids=[],
                user_sequence={},
                item_features={},
            ),
            3,
            route,
        )
    )
    assert empty.user_id == 8
    assert empty.items == []
    assert len(ranker.calls) == 1


def test_recommendation_observability_is_part_of_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = Mock()
    spans = Mock(side_effect=lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(ranking_module, "METRICS", metrics)
    monkeypatch.setattr(ranking_module, "span", spans)
    observed = []
    ranker = Ranker([0.25, 0.75])
    route = TritonRoute(ranker, "v2", "candidate", "exp")
    labels = {"ab_variant": "candidate", "experiment_id": "exp"}
    features = OnlineFeaturesResponse(
        user_id=7,
        candidate_item_ids=[10, 11],
        user_sequence={"hist_item_ids": [1]},
        item_features={"10": {"category_id": 1}, "11": {"category_id": 2}},
    )

    result = asyncio.run(
        recommend_from_online_features(
            features,
            1,
            route,
            metric_labels=labels,
            payload_observer=observed.append,
        )
    )

    assert result.items[0].model_dump() == {"item_id": 11, "score": 0.75}
    assert spans.call_args_list == [
        call("recommend.build_triton_payload", candidate_count=2),
        call("recommend.format_top_k", top_k=1),
    ]
    assert metrics.set_gauge.call_args_list == [
        call("recsys_api_candidate_count", 2, labels=labels),
        call("recsys_api_score_max", 0.75, labels=labels),
        call("recsys_api_recommendation_items_count", 1, labels=labels),
    ]
    metrics.observe.assert_called_once_with("recsys_api_score_mean", 0.5, labels=labels)
    assert len(observed) == 1

    metrics.reset_mock()
    spans.reset_mock()
    empty = asyncio.run(
        recommend_from_online_features(
            OnlineFeaturesResponse(
                user_id=8,
                candidate_item_ids=[],
                user_sequence={},
                item_features={},
            ),
            3,
            route,
            metric_labels=labels,
        )
    )
    assert empty.model_dump() == {
        "user_id": 8,
        "model_version": "v2",
        "ab_variant": "candidate",
        "ab_experiment_id": "exp",
        "items": [],
    }
    metrics.set_gauge.assert_called_once_with(
        "recsys_api_candidate_count", 0, labels=labels
    )
    metrics.inc.assert_called_once_with(
        "recsys_api_empty_recommendations_total", labels=labels
    )
    spans.assert_not_called()


class FakeResponse:
    def __init__(self) -> None:
        self.raised = 0

    def raise_for_status(self) -> None:
        self.raised += 1

    def json(self) -> dict:
        return {
            "user_id": 7,
            "candidate_item_ids": [101, 102],
            "user_sequence": {"hist_item_ids": [1]},
            "item_features": {"101": {}, "102": {}},
        }


class InjectedHttpClient:
    def __init__(self) -> None:
        self.get_calls = []
        self.post_calls = []
        self.response = FakeResponse()

    async def get(self, url, params):
        self.get_calls.append((url, params))
        return self.response

    async def post(self, url, json):
        self.post_calls.append((url, json))
        return self.response


def test_feature_client_get_post_and_url_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedAsyncClient:
        def __init__(self, timeout) -> None:
            raise RuntimeError("injected HTTP client was not reused")

    monkeypatch.setattr(
        feature_client_module.httpx, "AsyncClient", UnexpectedAsyncClient
    )
    http = InjectedHttpClient()
    client = OnlineFeatureServiceClient(
        "http://feature-api/", timeout_seconds=1.5, client=http
    )
    assert client.client is http
    explicit = asyncio.run(
        client.fetch(
            OnlineFeaturesRequest(user_id=7, candidate_item_ids=[101, 102], top_k=2)
        )
    )
    fallback = asyncio.run(client.fetch(OnlineFeaturesRequest(user_id=7, top_k=2)))
    assert explicit == fallback
    assert client.base_url == "http://feature-api"
    assert client.timeout_seconds == 1.5
    assert http.post_calls == [
        (
            "http://feature-api/online-features",
            {"user_id": 7, "candidate_item_ids": [101, 102], "top_k": 2},
        )
    ]
    assert http.get_calls == [("http://feature-api/online-features/7", {"top_k": 2})]
    assert http.response.raised == 2


def test_feature_client_default_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_API_URL", raising=False)
    monkeypatch.delenv("FEATURE_API_TIMEOUT_SECONDS", raising=False)
    client = OnlineFeatureServiceClient(client=InjectedHttpClient())
    assert client.base_url == "http://recsys-online-feature-api"
    assert client.timeout_seconds == 5.0


def test_feature_client_observability_and_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = Mock()
    spans = Mock(side_effect=lambda *args, **kwargs: nullcontext())
    clock = iter([2.0, 5.0, 10.0, 14.0])
    monkeypatch.setattr(feature_client_module, "METRICS", metrics)
    monkeypatch.setattr(feature_client_module, "span", spans)
    monkeypatch.setattr(feature_client_module.time, "perf_counter", lambda: next(clock))

    class ForbiddenAsyncClient:
        def __init__(self, timeout) -> None:
            self.response = FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            pass

        async def get(self, url, params):
            return self.response

        async def post(self, url, json):
            return self.response

    monkeypatch.setattr(
        feature_client_module.httpx, "AsyncClient", ForbiddenAsyncClient
    )
    http = InjectedHttpClient()
    client = OnlineFeatureServiceClient("http://feature", 2.0, client=http)
    asyncio.run(client.fetch(OnlineFeaturesRequest(user_id=7, top_k=2)))
    spans.assert_called_once_with(
        "feature_api.fetch_online_features", user_id=7, top_k=2
    )
    metrics.observe.assert_called_once_with(
        "recsys_feature_api_client_request_duration_seconds",
        3.0,
        labels={"status": "success"},
    )

    class BrokenHttp:
        async def get(self, url, params):
            raise RuntimeError("down")

    metrics.reset_mock()
    broken = BrokenHttp()
    injected = OnlineFeatureServiceClient("http://feature", 2.0, client=broken)
    assert injected.client is broken
    with pytest.raises(RuntimeError, match="down"):
        asyncio.run(injected.fetch(OnlineFeaturesRequest(user_id=8, top_k=3)))
    metrics.observe.assert_called_once_with(
        "recsys_feature_api_client_request_duration_seconds",
        4.0,
        labels={"status": "error"},
    )


def test_ab_routing_boundaries_shadow_and_close() -> None:
    class OwnedRanker:
        def __init__(self) -> None:
            self.aclose = AsyncMock()

        async def score(self, payload):
            return [], []

    control = OwnedRanker()
    candidate = OwnedRanker()
    stable = TritonABRouter(control, "control-v1")
    assert stable.assign(1) == "control"
    stable_route = stable.route(1)
    assert stable_route.ranker is control
    assert stable_route.ab_variant is None
    assert stable.shadow_route(1) is None

    all_candidate = TritonABRouter(
        control,
        "control-v1",
        candidate,
        "candidate-v2",
        enabled=True,
        candidate_weight_percent=101,
        experiment_id="exp",
    )
    assert all_candidate.candidate_weight_percent == 100
    assert all_candidate.assign(7) == "candidate"
    assert all_candidate.route(7).ranker is candidate

    none_candidate = TritonABRouter(
        control,
        "control-v1",
        candidate,
        "candidate-v2",
        enabled=True,
        candidate_weight_percent=-1,
    )
    assert none_candidate.candidate_weight_percent == 0
    assert none_candidate.assign(7) == "control"

    shadow = TritonABRouter(
        control,
        "control-v1",
        candidate,
        "candidate-v2",
        shadow_enabled=True,
        shadow_sample_percent=100,
        experiment_id="shadow-exp",
    )
    shadow_route = shadow.shadow_route(7)
    assert shadow_route is not None
    assert shadow_route.ranker is candidate
    assert shadow_route.model_version == "candidate-v2"
    assert shadow_route.ab_variant == "shadow_candidate"
    assert shadow_route.ab_experiment_id == "shadow-exp"
    assert shadow.route(7).ab_variant == "control"

    assert select_triton_route(control, 1, "fallback-v").model_version == "fallback-v"
    assert (
        select_triton_route(all_candidate, 1, "ignored").model_version == "candidate-v2"
    )

    asyncio.run(all_candidate.aclose())
    control.aclose.assert_awaited_once()
    candidate.aclose.assert_awaited_once()


def test_ab_router_exact_configuration_metrics_and_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = Mock()
    labels = Mock(return_value={"route": "label"})
    monkeypatch.setattr(ab_module, "METRICS", metrics)
    monkeypatch.setattr(ab_module, "ab_labels", labels)

    class OwnedRanker:
        def __init__(self) -> None:
            self.aclose = AsyncMock()

        async def score(self, payload):
            return [], []

    control = OwnedRanker()
    candidate_ranker = OwnedRanker()
    stable = TritonABRouter(control, "control")
    assert stable.control_ranker is control
    assert stable.control_model_version == "control"
    assert stable.candidate_ranker is None
    assert stable.candidate_model_version == "control"
    assert stable.enabled is False
    assert stable.shadow_enabled is False
    assert stable.candidate_weight_percent == 0
    assert stable.shadow_sample_percent == 100
    assert stable.experiment_id == "default"
    metrics.set_gauge.assert_called_once_with(
        "recsys_api_rollout_config_info",
        1,
        labels={
            "mode": "stable",
            "experiment_id": "default",
            "control_model_version": "control",
            "candidate_model_version": "none",
            "candidate_weight_percent": "0",
        },
    )

    metrics.reset_mock()
    router = TritonABRouter(
        control,
        "control",
        candidate_ranker,
        "candidate",
        enabled=False,
        candidate_weight_percent=55,
        experiment_id="exp",
        shadow_enabled=True,
        shadow_sample_percent=25,
    )
    assert router.enabled is False
    assert router.shadow_enabled is True
    assert router.candidate_weight_percent == 55
    assert router.shadow_sample_percent == 25
    metrics.set_gauge.assert_called_once_with(
        "recsys_api_rollout_config_info",
        1,
        labels={
            "mode": "shadow",
            "experiment_id": "exp",
            "control_model_version": "control",
            "candidate_model_version": "candidate",
            "candidate_weight_percent": "0",
        },
    )

    router._bucket = Mock(return_value=24)
    assert router.shadow_route(7) is not None
    router._bucket.assert_called_once_with(7, "shadow")
    router._bucket = Mock(return_value=25)
    assert router.shadow_route(7) is None

    metrics.reset_mock()
    route = router.route(7)
    assert route.ranker is control
    assert route.model_version == "control"
    assert route.ab_variant == "control"
    assert route.ab_experiment_id == "exp"
    labels.assert_called_with("control", "control", "exp")
    metrics.inc.assert_called_once_with(
        "recsys_api_ab_assignments_total", labels={"route": "label"}
    )

    shared = OwnedRanker()
    duplicate = TritonABRouter(shared, "control", shared, "candidate", enabled=True)
    asyncio.run(duplicate.aclose())
    shared.aclose.assert_awaited_once()


def test_ab_bucket_is_sticky_and_bounded() -> None:
    router = TritonABRouter(
        AsyncMock(),
        "v1",
        AsyncMock(),
        "v2",
        enabled=True,
        candidate_weight_percent=50,
        experiment_id="exp-1",
    )
    first = router._bucket(42, "ab")
    assert 0 <= first < 100
    assert router._bucket(42, "ab") == first
    assert router._bucket(42, "shadow") != first
    assert first == int(hashlib.sha256(b"exp-1:42").hexdigest()[:8], 16) % 100
    assert router._bucket(42, "shadow") == (
        int(hashlib.sha256(b"shadow:exp-1:42").hexdigest()[:8], 16) % 100
    )


def test_shadow_runner_defaults_clamps_and_drop_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = Mock()
    labels = Mock(return_value={"route": "shadow"})
    monkeypatch.setattr(shadow_module, "METRICS", metrics)
    monkeypatch.setattr(shadow_module, "ab_labels", labels)

    defaults = ShadowRunner()
    assert defaults.timeout_seconds == 1.0
    assert defaults.max_pending == 100
    assert defaults._semaphore._value == 4
    assert defaults.pending_count == 0
    clamped = ShadowRunner(timeout_seconds=0, max_pending=0, max_concurrency=0)
    assert clamped.timeout_seconds == 0.001
    assert clamped.max_pending == 1
    assert clamped._semaphore._value == 1

    clamped._tasks.add(object())  # type: ignore[arg-type]
    route = TritonRoute(Ranker([1.0]), "candidate", "shadow_candidate", "exp")
    assert clamped.submit(route, {}) is False
    labels.assert_called_once_with("shadow_candidate", "candidate", "exp")
    metrics.inc.assert_called_once_with(
        "recsys_api_shadow_inferences_total",
        labels={"route": "shadow", "status": "dropped"},
    )
    metrics.set_gauge.assert_called_once_with(
        "recsys_api_shadow_queue_depth", 1, labels={"route": "shadow"}
    )


def test_shadow_run_statuses_and_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        metrics = Mock()
        monkeypatch.setattr(shadow_module, "METRICS", metrics)
        clock = iter([10.0, 11.0, 12.0, 13.0])
        monkeypatch.setattr(shadow_module.time, "perf_counter", lambda: next(clock))
        runner = ShadowRunner(timeout_seconds=5)
        route = TritonRoute(Ranker([0.25, 0.75]), "candidate", None, "exp")
        labels = {"route": "shadow"}
        await runner._run(route, {"candidate_item_id": np.asarray([1, 2])}, labels)
        metrics.observe.assert_called_once_with(
            "recsys_api_shadow_score_mean", 0.5, labels=labels
        )
        metrics.set_gauge.assert_any_call(
            "recsys_api_shadow_score_max", 0.75, labels=labels
        )
        metrics.inc.assert_called_once_with(
            "recsys_api_shadow_inferences_total",
            labels={"route": "shadow", "status": "success"},
        )
        metrics.observe_histogram.assert_called_once_with(
            "recsys_api_shadow_latency_seconds",
            3.0,
            labels=labels,
            buckets=shadow_module.LATENCY_BUCKETS,
        )
        metrics.set_gauge.assert_any_call(
            "recsys_api_shadow_queue_depth", 0, labels=labels
        )

        metrics.reset_mock()
        clock = iter([20.0, 21.0, 22.0, 23.0])
        monkeypatch.setattr(shadow_module.time, "perf_counter", lambda: next(clock))
        threshold = ShadowRunner(timeout_seconds=1)
        await threshold._run(route, {"candidate_item_id": np.asarray([1, 2])}, labels)
        metrics.inc.assert_called_once_with(
            "recsys_api_shadow_inferences_total",
            labels={"route": "shadow", "status": "timeout"},
        )

        class BrokenRanker:
            async def score(self, payload):
                raise ValueError("broken")

        metrics.reset_mock()
        clock = iter([30.0, 31.0, 32.0])
        monkeypatch.setattr(shadow_module.time, "perf_counter", lambda: next(clock))
        await runner._run(TritonRoute(BrokenRanker(), "candidate"), {}, labels)
        metrics.inc.assert_called_once_with(
            "recsys_api_shadow_inferences_total",
            labels={"route": "shadow", "status": "error"},
        )

    asyncio.run(scenario())


def test_shadow_runner_success_none_and_queue_drop() -> None:
    async def scenario() -> None:
        runner = ShadowRunner(timeout_seconds=1, max_pending=1, max_concurrency=1)
        assert runner.timeout_seconds == 1.0
        assert runner.max_pending == 1
        assert runner.submit(None, {}) is False

        blocker = asyncio.Event()

        class SlowRanker:
            async def score(self, payload):
                await blocker.wait()
                return [1], [0.75]

        route = TritonRoute(SlowRanker(), "candidate", "shadow_candidate", "exp")
        assert runner.submit(route, {"candidate_item_id": np.asarray([1])}) is True
        assert runner.pending_count == 1
        assert runner.submit(route, {}) is False
        blocker.set()
        await runner.drain()
        assert runner.pending_count == 0

    asyncio.run(scenario())


class FakeInferInput:
    def __init__(self, name, shape, dtype) -> None:
        self.name = name
        self.shape = shape
        self.dtype = dtype
        self.values = None

    def set_data_from_numpy(self, values) -> None:
        self.values = values


class FakeGrpc:
    InferInput = FakeInferInput

    @staticmethod
    def InferRequestedOutput(name):
        return name


class FakeResult:
    def as_numpy(self, name):
        if name == "candidate_item_id_out":
            return np.asarray([[3, 4]])
        return np.asarray([[0.25, 0.75]])


class FakeTritonClient:
    def __init__(self) -> None:
        self.calls = []
        self.closed = 0

    async def infer(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResult()

    async def close(self):
        self.closed += 1


def test_triton_adapter_builds_inputs_outputs_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observe = Mock()
    spans = Mock(side_effect=lambda *args, **kwargs: nullcontext())
    clock = iter([2.0, 5.0])
    monkeypatch.setattr(triton_module, "observe_triton", observe)
    monkeypatch.setattr(triton_module, "span", spans)
    monkeypatch.setattr(triton_module.time, "perf_counter", lambda: next(clock))
    ranker = TritonRanker.__new__(TritonRanker)
    ranker.grpcclient = FakeGrpc
    ranker.client = FakeTritonClient()
    ranker.model_name = "ensemble"
    ranker.model_version = "v3"
    ranker.ab_variant = "control"
    ranker.ab_experiment_id = "exp"
    ranker.timeout_seconds = 2.0

    @asynccontextmanager
    async def slot():
        yield

    ranker._capacity = type("Capacity", (), {"slot": staticmethod(slot)})()
    payload = {
        "candidate_item_id": np.asarray([3, 4], dtype=np.int64),
        "candidate_brand": np.asarray([1, 2], dtype=np.int64),
    }
    item_ids, scores = asyncio.run(ranker.score(payload))
    assert item_ids == [3, 4]
    assert scores == [0.25, 0.75]
    call = ranker.client.calls[0]
    assert call["model_name"] == "ensemble"
    assert call["outputs"] == ["candidate_item_id_out", "score"]
    assert call["client_timeout"] == 2.0
    assert [value.name for value in call["inputs"]] == [
        "candidate_item_id",
        "candidate_brand",
    ]
    spans.assert_called_once_with("triton.infer", model_name="ensemble", input_count=2)
    observe.assert_called_once_with(
        "ensemble",
        3.0,
        labels={
            "ab_variant": "control",
            "model_version": "v3",
            "experiment_id": "exp",
        },
    )
    asyncio.run(ranker.aclose())
    assert ranker.client.closed == 1
