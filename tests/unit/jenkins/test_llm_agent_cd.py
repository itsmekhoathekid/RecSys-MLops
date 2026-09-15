from copy import deepcopy
from pathlib import Path
import json

import pytest

from jenkins.python.llm_agent_cd.engine import Engine
from jenkins.python.llm_agent_cd.evidence import inspect
from jenkins.python.llm_agent_cd.gates import case_gate, production_gate
from jenkins.python.llm_agent_cd.manifests import (
    backend_resources,
    resources,
    virtual_service,
)
from jenkins.python.llm_agent_cd.release import (
    DEFAULT_POLICY,
    digest,
    policy,
    release,
    validate_experiment,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def champion():
    return release(
        {
            "config": {"temperature": "0", "maxTokens": 384, "seed": 42},
            "llm": {
                "artifact_uri": "https://example.com/model.gguf",
                "artifact_sha256": "a" * 64,
                "quantization": "Q4_0",
                "image": "llama@sha256:" + "b" * 64,
                "serving": {
                    "contextSize": 16384,
                    "maxPredictedTokens": 768,
                    "reasoningBudget": 256,
                    "parallel": 1,
                    "threads": 2,
                    "threadsBatch": 2,
                    "batchSize": 512,
                    "ubatchSize": 128,
                },
            },
            "agent": {
                "runtime": "go",
                "systemMessage": "Keep ranking.",
                "tools": [{"type": "McpServer"}],
            },
            "binding": {
                "backend_url": "http://baseline/v1",
                "worker_pool": "pool",
                "model_alias": "qwen",
                "api_key_secret": "secret",
                "api_key_secret_key": "key",
                "allowed_domains": ["baseline"],
            },
        }
    )


def change(champion, config=False, llm=False):
    r = {k: deepcopy(v) for k, v in champion.items() if not k.endswith("_id")}
    if config:
        r["config"]["temperature"] = "0.3"
    if llm:
        r["llm"]["artifact_sha256"] = "c" * 64
        r["binding"]["backend_url"] = "http://candidate/v1"
    return release(r)


@pytest.mark.parametrize(
    "mode,config_changed,llm_changed",
    [("config_only", True, False), ("llm_only", False, True), ("combined", True, True)],
)
def test_three_axes(champion, mode, config_changed, llm_changed):
    validate_experiment(champion, change(champion, config_changed, llm_changed), mode)
    for wrong in {"config_only", "llm_only", "combined"} - {mode}:
        with pytest.raises(ValueError):
            validate_experiment(
                champion, change(champion, config_changed, llm_changed), wrong
            )


def test_prompt_changes_and_hash_tampering_rejected(champion):
    r = change(champion, True)
    r["agent"]["systemMessage"] += " changed"
    with pytest.raises(ValueError):
        validate_experiment(champion, r, "config_only")
    r.pop("release_id")
    with pytest.raises(ValueError, match="prompt"):
        validate_experiment(champion, release(r), "config_only")


def test_config_identity_excludes_backend(champion):
    other = change(champion, llm=True)
    assert other["config_id"] == champion["config_id"]
    with pytest.raises(ValueError):
        policy({**DEFAULT_POLICY, "case_count": 40})
    with pytest.raises(ValueError):
        policy({**DEFAULT_POLICY, "latency_ratio": float("nan")})


def test_binding_schema_v2_prevents_immutable_release_collision(champion):
    legacy = {k: deepcopy(v) for k, v in champion.items() if not k.endswith("_id")}
    changed_legacy = deepcopy(legacy)
    changed_legacy["binding"]["model_alias"] = "other-alias"
    assert release(legacy)["release_id"] == release(changed_legacy)["release_id"]

    legacy["binding"]["release_schema_version"] = 2
    changed_legacy["binding"]["release_schema_version"] = 2
    assert release(legacy)["release_id"] != release(changed_legacy)["release_id"]


def test_binding_schema_v3_binds_sandbox_network_policy(champion):
    first = {k: deepcopy(v) for k, v in champion.items() if not k.endswith("_id")}
    second = deepcopy(first)
    first["binding"]["release_schema_version"] = 3
    second["binding"]["release_schema_version"] = 3
    second["binding"]["allowed_domains"] = ["candidate.kagent.svc.cluster.local"]

    assert release(first)["release_id"] != release(second)["release_id"]


def test_legacy_adapter_manifest_keeps_legacy_transport_shape(champion):
    adapter = next(
        item
        for item in resources(champion, "kagent", "router@sha256:" + "d" * 64, "runtime")
        if item["kind"] == "Deployment"
    )
    assert adapter["spec"]["replicas"] == 2
    assert "AB_KAGENT_GRPC_TARGET" not in {
        item["name"]
        for item in adapter["spec"]["template"]["spec"]["containers"][0]["env"]
    }


def observation(count=5):
    return {
        "healthy": True,
        **{
            arm: {
                "count": count,
                "errors": 0,
                "contract_failures": 0,
                "unknown": 0,
                "p95": 2.0,
            }
            for arm in ("champion", "candidate")
        },
    }


@pytest.mark.parametrize(
    "field,value", [("p95", None), ("p95", float("nan")), ("count", 4), ("unknown", 1)]
)
def test_missing_or_insufficient_is_hold(field, value):
    o = observation()
    o["candidate"][field] = value
    assert production_gate(o, DEFAULT_POLICY, compare=True)[0] == "HOLD"


@pytest.mark.parametrize('bad_arm', ['champion', 'candidate'])
@pytest.mark.parametrize('field', ['errors', 'contract_failures'])
@pytest.mark.parametrize('healthy', [True, False])
def test_known_fault_precedes_other_arm_missing_samples(bad_arm, field, healthy):
    o = observation(count=0)
    o['healthy'] = healthy
    o[bad_arm][field] = 1
    verdict, reason = production_gate(o, DEFAULT_POLICY, compare=False)
    assert verdict == 'FAIL'
    assert bad_arm in reason


def test_failure_and_latency_gate():
    o = observation()
    o["candidate"]["p95"] = 2.41
    assert production_gate(o, DEFAULT_POLICY, compare=True)[0] == "FAIL"
    o["candidate"]["errors"] = 1
    assert production_gate(o, DEFAULT_POLICY, compare=False) == (
        "FAIL",
        "candidate runtime error",
    )


def test_runtime_and_contract_failures_have_distinct_gate_reasons():
    runtime = observation()
    runtime["champion"]["errors"] = 1
    assert production_gate(runtime, DEFAULT_POLICY, compare=False) == (
        "FAIL",
        "champion runtime error",
    )

    contract = observation()
    contract["candidate"]["contract_failures"] = 1
    assert production_gate(contract, DEFAULT_POLICY, compare=False) == (
        "FAIL",
        "candidate tool contract violation",
    )

    combined = observation()
    combined["candidate"]["errors"] = 1
    combined["candidate"]["contract_failures"] = 1
    assert production_gate(combined, DEFAULT_POLICY, compare=False) == (
        "FAIL",
        "candidate runtime error and tool contract violation",
    )


@pytest.mark.parametrize(
    "candidate_count,verdict", [(4, "HOLD"), (5, "PASS"), (10, "PASS"), (16, "HOLD")]
)
def test_probabilistic_split_not_padded(candidate_count, verdict):
    cases = {
        str(i): {"verdict": "PASS", "release_id": "B" if i < candidate_count else "A"}
        for i in range(20)
    }
    assert case_gate(cases, "A", "B")[0] == verdict


class MemoryStore:
    def __init__(self, champion):
        self.value, self.etag = {"phase": "IDLE", "champion": champion}, 0

    def read(self):
        return deepcopy(self.value), self.etag

    def write(self, state, etag):
        if self.etag != etag:
            raise RuntimeError("CAS conflict")
        self.etag += 1
        self.value = deepcopy(state)
        return self.etag


class FakeDriver:
    def __init__(self):
        self.calls, self.weights, self.results = [], [], {}
        self.ack = True
        self.o = observation()
        self.crash_send = False
        self.inflight = {"live_test": 0, "synthetic": 0}
        self.external_sent = False

    def preflight(self, *args):
        pass

    def snapshot_route(self):
        return {"old": 100}

    def deploy(self, *args):
        pass

    def verify_release(self, *args):
        pass

    def route(self, state, weight):
        self.weights.append(weight)
        return f"revision-{weight}"

    def verify_route(self, *args):
        return self.ack

    def observe(self, *args):
        return deepcopy(self.o)

    def live_load(self, *args):
        pass

    def source_inflight(self, state, source):
        return self.inflight[source]

    def send_case(self, state, case, request_id):
        self.calls.append(request_id)
        if self.crash_send:
            raise SystemExit("process killed after claim")
        r = state["baseline"] if len(self.calls) % 2 else state["pending"]
        self.results[request_id] = {"verdict": "PASS", "release_id": r["release_id"]}

    def send_external_suite(self, state):
        if self.external_sent:
            return
        self.external_sent = True
        self.calls.append("external-suite")
        for index, case in enumerate(state["fixtures"]):
            request_id = digest([state["experiment_id"], case["id"]])
            release = state["baseline"] if index % 2 else state["pending"]
            self.results[request_id] = {
                "verdict": "PASS", "release_id": release["release_id"]
            }

    def external_suite_evidence(self, state):
        return {"run": {"status": "COMPLETED"}, "tickets": {"COMPLETED": 20}}

    def case_result(self, key):
        return self.results.get(key)


def setup_engine(champion):
    now = [1000]
    store, driver = MemoryStore(champion), FakeDriver()
    e = Engine(store, driver, lambda: now[0])
    e.start(
        change(champion, True),
        "config_only",
        DEFAULT_POLICY,
        json.loads((ROOT / "configs/llm-ab/cases.json").read_text()),
        "experiment-1",
    )
    e.tick()  # Deploy => ROUTING 10.
    e.tick()  # Ack => CANARY.
    now[0] += 600
    e.tick()  # CANARY => ROUTING 50.
    e.tick()  # Ack => AB.
    return e, store, driver, now


def test_recommendation_retry_reenables_candidate_before_zero_weight_prepare(champion):
    candidate = change(champion, llm=True)
    store, driver = MemoryStore(champion), FakeDriver()
    store.value["disabled"] = [candidate["release_id"]]

    engine = Engine(store, driver, lambda: 1000)
    engine.start(
        candidate,
        "llm_only",
        DEFAULT_POLICY,
        json.loads((ROOT / "configs/llm-ab/cases.json").read_text()),
        "experiment-retry",
    )

    assert candidate["release_id"] not in engine.state["disabled"]
    assert engine.state["verified_weight"] is None
    assert engine.state["route_intent"]["weight"] == 0


def test_rollout_promote_monitor_and_composite_rollback(champion):
    e, store, driver, now = setup_engine(champion)
    for _ in range(20):
        e.tick()
    assert len(driver.calls) == len(set(driver.calls)) == 20
    now[0] += 600
    e.tick()
    assert e.state["phase"] == "ROUTING"
    e.tick()
    assert e.state["phase"] == "VERIFY"
    now[0] += 600
    e.tick()
    assert e.state["phase"] == "MONITOR"
    assert e.state["champion"] == e.state["pending"]
    driver.o["candidate"]["errors"] = 1
    for _ in range(2):
        now[0] += 300
        e = Engine(store, driver, lambda: now[0])
        e.tick()
    assert e.state["phase"] == "ROLLED_BACK"
    assert e.state["champion"] == champion
    assert driver.weights == [10, 50, 100, 0]


def test_crash_never_replays_started_case(champion):
    e, store, driver, now = setup_engine(champion)
    driver.crash_send = True
    with pytest.raises(SystemExit):
        e.tick()
    driver.crash_send = False
    e = Engine(store, driver, lambda: now[0])
    for _ in range(25):
        e.tick()
    assert len(driver.calls) == len(set(driver.calls)) == 20
    assert e.state["gate"]["verdict"] == "HOLD"
    now[0] += 3601
    e.tick()
    assert e.state["phase"] == "ROLLED_BACK"


def test_ab_drains_live_load_before_sending_exact_suite(champion):
    now = [1000]
    store, driver = MemoryStore(champion), FakeDriver()
    e = Engine(store, driver, lambda: now[0])
    e.start(
        change(champion, True),
        "config_only",
        {**DEFAULT_POLICY, "sample_source": "live_test"},
        json.loads((ROOT / "configs/llm-ab/cases.json").read_text()),
        "experiment-live-drain",
    )
    e.tick()
    e.tick()
    now[0] += 600
    e.tick()
    e.tick()
    assert e.state["phase"] == "AB"
    driver.inflight["live_test"] = 1
    e.tick()
    assert driver.calls == []
    driver.inflight["live_test"] = 0
    e.tick()
    assert len(driver.calls) == 1


def test_public_a2a_policy_dispatches_one_suite_not_internal_cases(champion):
    now = [1000]
    store, driver = MemoryStore(champion), FakeDriver()
    e = Engine(store, driver, lambda: now[0])
    e.start(
        change(champion, True), "config_only",
        {**DEFAULT_POLICY, "synthetic_entrypoint": "public_a2a"},
        json.loads((ROOT / "configs/llm-ab/cases.json").read_text()),
        "experiment-public-edge",
    )
    e.tick(); e.tick()
    now[0] += 600
    e.tick(); e.tick(); e.tick()
    assert driver.calls == ["external-suite"]
    assert len(e.state["cases"]) == 20


def test_compatibility_experiment_id_is_rejected_before_state_mutation(champion):
    store, driver = MemoryStore(champion), FakeDriver()
    before = deepcopy(store.value)
    e = Engine(store, driver, lambda: 1000)
    rules = {
        **DEFAULT_POLICY,
        "sample_source": "live_test",
        "compatibility_suite": "recommendation-compatibility-smoke-v2",
    }
    fixtures = json.loads((ROOT / "configs/llm-ab/cases.json").read_text())
    with pytest.raises(ValueError, match="finite compatibility Job identity"):
        e.start(change(champion, True), "config_only", rules, fixtures, "descriptive-id")
    assert store.value == before


def test_route_ack_and_rollback_failure(champion):
    e, store, driver, now = setup_engine(champion)
    driver.ack = False
    e.rollback("failure")
    assert e.state["phase"] == "ROLLING_BACK"
    now[0] += 121
    e.tick()
    assert e.state["phase"] == "ROLLBACK_FAILED"
    assert e.state["champion"] == champion


def test_concurrent_writer_prevents_route_mutation(champion):
    e, store, driver, now = setup_engine(champion)
    store.write(store.value, store.etag)
    weights = list(driver.weights)
    with pytest.raises(RuntimeError, match="CAS"):
        e.rollback("stale owner")
    assert driver.weights == weights


def test_shared_backend_and_routing_manifest(champion):
    candidate = change(champion, True)
    assert backend_resources(candidate, "kagent", "router-image") == []
    specs = resources(candidate, "kagent", "router-image", "secret")
    assert (
        specs[1]["spec"]["declarative"]["systemMessage"]
        == champion["agent"]["systemMessage"]
    )
    state = {"baseline": champion, "pending": candidate, "disabled": []}
    vs = virtual_service(state, 50, "kagent")
    allocation = vs["spec"]["http"][-2]
    assert [r["weight"] for r in allocation["route"]] == [50, 50]
    assert allocation["retries"] == {"attempts": 0}
    state["disabled"] = [candidate["release_id"]]
    vs = virtual_service(state, 0, "kagent")
    assert len(vs["spec"]["http"][-2]["route"]) == 1
    assert all(
        candidate["release_id"] not in json.dumps(r.get("match"))
        for r in vs["spec"]["http"]
    )


def task_fixture():
    args = {"user_id": 1001, "candidate_item_ids": None, "top_k": 3}
    source = {
        "user_id": 1001,
        "items": [{"item_id": 3, "score": 0.8}, {"item_id": 4, "score": 0.7}],
        "model_version": "BST",
        "ab_variant": "A",
        "ab_experiment_id": None,
    }
    call = {
        "metadata": {"adk_type": "function_call"},
        "data": {"name": "get_personalized_recommendations", "args": args},
    }
    response = {
        "metadata": {"adk_type": "function_response"},
        "data": {"name": "get_personalized_recommendations", "response": source},
    }
    body = {
        "result": {
            "task": {
                "id": "task-1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "history": [{"parts": [call, response]}],
                "artifacts": [{"parts": [{"text": json.dumps(source)}]}],
            }
        }
    }
    return body, {"arguments": args}


def test_artifact_tool_events_and_token_usage_from_pinned_runtime():
    body, expected = task_fixture()
    task = body["result"]["task"]
    events = task["history"][0]["parts"]
    for event in events:
        event["data"]["id"] = "call-1"
    task["artifacts"].extend(
        [
            {
                "artifactId": "call",
                "parts": [events[0]],
                "metadata": {
                    "adk_usage_metadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 2,
                    }
                },
            },
            {"artifactId": "response", "parts": [events[1]]},
        ]
    )
    # Duplicate history/artifact representation is one call, not two calls.
    assert inspect(body, expected)["verdict"] == "PASS"
    task["history"] = [
        {
            "messageId": "request-1",
            "role": "ROLE_USER",
            "parts": [{"text": "recommend"}],
        }
    ]
    result = inspect(body, expected, "request-1")
    assert result["verdict"] == "PASS"
    assert result["input_tokens"] == 10 and result["output_tokens"] == 2
    task["artifacts"].append({"parts": [deepcopy(events[0])]})
    assert inspect(body, expected)["verdict"] == "FAIL"


def test_adapter_image_pin_survives_control_plane_upgrade(champion):
    champion["binding"]["adapter_image"] = "old@sha256:" + "1" * 64
    objects = resources(champion, "kagent", "new@sha256:" + "2" * 64, "secret")
    deployment = next(o for o in objects if o["kind"] == "Deployment")
    assert (
        deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        == champion["binding"]["adapter_image"]
    )


def test_tool_evidence_checks_order_metadata_and_double_call():
    body, expected = task_fixture()
    assert inspect(body, expected)["verdict"] == "PASS"
    task = body["result"]["task"]
    output = json.loads(task["artifacts"][0]["parts"][0]["text"])
    output["items"].reverse()
    task["artifacts"][0]["parts"][0]["text"] = json.dumps(output)
    assert inspect(body, expected)["verdict"] == "FAIL"
    body, expected = task_fixture()
    body["result"]["task"]["history"][0]["parts"].append(
        body["result"]["task"]["history"][0]["parts"][0]
    )
    assert inspect(body, expected)["verdict"] == "FAIL"


def test_missing_trace_or_unparseable_answer_is_not_success():
    body, expected = task_fixture()
    body["result"]["task"]["history"] = []
    assert inspect(body, expected)["verdict"] == "HOLD"


def test_missing_user_terminal_json_passes_without_any_function_call():
    body = {
        "result": {
            "task": {
                "status": {"state": "TASK_STATE_COMPLETED"},
                "history": [
                    {
                        "messageId": "missing-user",
                        "role": "ROLE_USER",
                        "parts": [{"text": "recommend without an ID"}],
                    }
                ],
                "artifacts": [
                    {
                        "parts": [
                            {
                                "text": '{"status":"clarification_required","missing":["user_id"]}'
                            }
                        ]
                    }
                ],
            }
        }
    }
    result = inspect(body, {"missing_user": True}, "missing-user")
    assert result["verdict"] == "PASS"
    assert result["reason"] == "terminal clarification without dependency execution"
    body, expected = task_fixture()
    body["result"]["task"]["artifacts"][0]["parts"][0]["text"] = "Try item 3."
    assert inspect(body, expected)["verdict"] == "HOLD"


def test_fixtures_are_exactly_twenty():
    from collections import Counter

    cases = json.loads((ROOT / "configs/llm-ab/cases.json").read_text())
    assert len(cases) == len({c["id"] for c in cases}) == 20
    assert Counter(c["category"] for c in cases) == {
        "normal": 8,
        "limit": 4,
        "metadata": 4,
        "missing_user": 2,
        "empty": 2,
    }


def promoted_engine(champion):
    e, store, driver, now = setup_engine(champion)
    for _ in range(20):
        e.tick()
    now[0] += 600
    e.tick()
    e.tick()
    now[0] += 600
    e.tick()
    assert e.state["phase"] == "MONITOR"
    return e, store, driver, now


def test_healthy_no_traffic_is_not_failure(champion):
    e, store, driver, now = promoted_engine(champion)
    driver.o = observation(0)
    for _ in range(2):
        now[0] += 300
        e.tick()
    assert e.state["phase"] == "MONITOR"
    assert e.state["bad_windows"] == e.state["telemetry_failures"] == 0
    now[0] += 86400
    e.tick()
    assert e.state["phase"] == "COMPLETED"


def test_bootstrap_rollback_cannot_quarantine_champion(champion):
    e, store, driver, now = setup_engine(champion)
    e.state.update(phase="IDLE", pending=champion, baseline=champion)
    with pytest.raises(ValueError, match="no distinct challenger"):
        e.rollback("operator clicked rollback before starting an experiment")
    assert e.state["phase"] == "IDLE"
    assert champion["release_id"] not in e.state.get("disabled", [])


def test_missing_telemetry_twice_restores_composite_release(champion):
    e, store, driver, now = promoted_engine(champion)
    driver.o = {"healthy": False}
    now[0] += 300
    e.tick()
    assert e.state["phase"] == "MONITOR"
    now[0] += 300
    e.tick()
    assert e.state["phase"] == "ROLLED_BACK"
    assert e.state["champion"] == champion


def test_route_wait_does_not_advance_until_all_proxies_ack(champion):
    e, store, driver, now = setup_engine(champion)
    e.shift(100, "VERIFY")
    driver.ack = False
    e.tick()
    assert e.state["phase"] == "ROUTING"
    driver.ack = True
    e = Engine(store, driver, lambda: now[0])
    e.tick()
    assert e.state["phase"] == "VERIFY"
    assert not driver.calls


def test_s3_store_conditional_writes_and_versioning():
    import io
    from types import SimpleNamespace
    from jenkins.python.llm_agent_cd.state import StateStore

    class Client:
        meta = SimpleNamespace(
            service_model=SimpleNamespace(
                operation_model=lambda _: SimpleNamespace(
                    input_shape=SimpleNamespace(
                        members={"IfMatch": 1, "IfNoneMatch": 1}
                    )
                )
            )
        )

        def __init__(self):
            self.value = None
            self.etag = None
            self.versioning = "Enabled"
            self.writes = []

        def get_bucket_versioning(self, **kwargs):
            return {"Status": self.versioning}

        def put_object(self, **kwargs):
            self.writes.append(kwargs)
            if ("IfNoneMatch" in kwargs and self.value is not None) or (
                "IfMatch" in kwargs and kwargs["IfMatch"] != self.etag
            ):
                raise RuntimeError("PreconditionFailed")
            self.etag = str(len(self.writes))
            self.value = kwargs["Body"]
            return {"ETag": self.etag}

        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(self.value), "ETag": self.etag}

    client = Client()
    store = StateStore("s3://bucket/state.json", client)
    etag = store.write({"champion": "A"}, None)
    assert store.read() == ({"champion": "A"}, etag)
    assert client.writes[-1]["IfNoneMatch"] == "*"
    store.write({"champion": "B"}, etag)
    with pytest.raises(RuntimeError, match="PreconditionFailed"):
        store.write({"champion": "C"}, etag)
    assert len(client.writes) == 3  # No blind retry.
    client.versioning = "Suspended"
    with pytest.raises(RuntimeError, match="versioning"):
        StateStore("s3://bucket/state.json", client)
