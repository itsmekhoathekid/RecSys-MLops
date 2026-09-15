from copy import deepcopy

import pytest

from apps.agentic.llm_ab_router.trigger import (
    ACTIVE_LABEL,
    PARKING_LABEL,
    PARKING_PROMPT_TEXT,
    PROMPT_TEXT,
    READY_LABEL,
    Trigger,
    candidate_from_config,
    canonical_prompt_snapshot,
    dispatch_job,
    configured_experiment_pattern,
    jenkins_parameters,
    polling_mode,
    prompt_checksum,
    validate_config,
)
from jenkins.python.llm_agent_cd.release import digest, release, validate_experiment
pytest_plugins = ("tests.unit.jenkins.test_llm_agent_cd",)


@pytest.fixture
def request_config(champion, monkeypatch):
    monkeypatch.setenv("AB_ALLOW_LIVE_TEST", "true")
    return {
        "schema_version": 1,
        "scope": "recommendation",
        "baseline_release_id": champion["release_id"],
        "generation": {**deepcopy(champion["config"]), "temperature": "0.2"},
        "llm_release_ref": champion["llm_version_id"],
        "experiment_type": "config_only",
        "policy_ref": "recommendation-live-test",
    }


def test_recommendation_candidate_changes_only_generation(champion, request_config):
    validate_config(request_config)
    candidate = candidate_from_config(champion, request_config, champion["llm"])
    validate_experiment(champion, candidate, "config_only")
    assert candidate["config"]["temperature"] == "0.2"
    assert candidate["llm_version_id"] == champion["llm_version_id"]
    assert candidate["agent"] == champion["agent"]
    assert candidate["binding"] == champion["binding"]


def test_llm_candidate_uses_only_its_managed_backend_domain(champion, request_config):
    baseline_raw = {
        key: deepcopy(value)
        for key, value in champion.items()
        if not key.endswith("_id")
    }
    baseline_raw["binding"]["allowed_domains"] = [
        "rec-llm-" + "a" * 20 + ".kagent.svc.cluster.local",
        "recsys-recommendation-mcp.kagent.svc.cluster.local",
    ]
    baseline = release(baseline_raw)
    llm = deepcopy(champion["llm"])
    llm["artifact_sha256"] = "c" * 64
    request = {
        **request_config,
        "baseline_release_id": baseline["release_id"],
        "generation": deepcopy(baseline["config"]),
        "llm_release_ref": digest(llm),
        "experiment_type": "llm_only",
    }

    candidate = candidate_from_config(baseline, request, llm)

    assert candidate["binding"]["release_schema_version"] == 3
    assert candidate["binding"]["allowed_domains"] == [
        "rec-llm-" + candidate["llm_version_id"][:20] + ".kagent.svc.cluster.local",
        "recsys-recommendation-mcp.kagent.svc.cluster.local",
    ]


def test_recommendation_candidate_rejects_stale_or_wrong_scope(champion, request_config):
    stale = deepcopy(request_config)
    stale["baseline_release_id"] = "f" * 64
    with pytest.raises(ValueError, match="STALE_BASELINE"):
        candidate_from_config(champion, stale, champion["llm"])
    with pytest.raises(ValueError, match="schema"):
        validate_config({**request_config, "endpoint": "https://untrusted.example"})


def test_disabled_recommendation_candidate_can_retry_offline_gate(champion, request_config):
    candidate = candidate_from_config(champion, request_config, champion["llm"])
    assert Trigger.candidate_rejection(
        {"disabled": [candidate["release_id"]]}, candidate
    ) is None
    assert Trigger.candidate_rejection({"disabled": []}, candidate) is None


def test_recommendation_has_no_per_candidate_job_and_has_jenkins_parameters(
    champion, request_config, monkeypatch
):
    image = "registry.example/router@sha256:" + "b" * 64
    monkeypatch.setenv("AB_DISPATCH_IMAGE", image)
    monkeypatch.setenv("AB_RECOMMENDATION_ROUTER_IMAGE", image)
    monkeypatch.setenv("AB_TRIGGER_SECRET", "recsys-recommendation-trigger")
    with pytest.raises(ValueError, match="directly"):
        dispatch_job("rec-" + "a" * 32)

    params = jenkins_parameters(
        "recommendation", request_config, "s3://bucket/candidate.json"
    )
    assert params["POLICY"] == "configs/llm-ab/recommendation-live-test-policy.json"
    assert params["FIXTURES"] == "configs/llm-ab/cases.json"
    assert params["SOURCE_MODE"] == "deployed-image"
    assert params["ROUTER_IMAGE"] == image
    assert params["BASELINE_RELEASE_ID"] == champion["release_id"]


def test_dedicated_recovery_is_scoped(monkeypatch):
    monkeypatch.setenv("AB_TRIGGER_SCOPE", "recommendation")
    assert configured_experiment_pattern() == "rec-%"
    monkeypatch.setenv("AB_TRIGGER_SCOPE", "workflow")
    assert configured_experiment_pattern() == "wf-%"
    monkeypatch.delenv("AB_TRIGGER_SCOPE")
    assert configured_experiment_pattern() == "%"


def test_poll_identity_ignores_mutable_labels(monkeypatch, request_config):
    base = {"name": "recsys-recommendation-ab", "version": 7, "type": "text",
            "prompt": "immutable", "config": request_config, "labels": ["ab-ready"]}
    moved = {**base, "labels": ["ab-running", "latest"]}
    assert canonical_prompt_snapshot("project", base) == canonical_prompt_snapshot("project", moved)
    assert prompt_checksum("project", base) == prompt_checksum("project", moved)
    monkeypatch.setenv("AB_TRIGGER_MODE", "poll")
    monkeypatch.setenv("AB_TRIGGER_SCOPE", "recommendation")
    assert polling_mode()
    monkeypatch.setenv("AB_TRIGGER_SCOPE", "workflow")
    assert not polling_mode()


def test_recommendation_chart_is_tokenless_poller_only():
    chart = open("infra/helm/recsys-llm-ab/templates/trigger.yaml").read()
    assert "kind: CronJob" in chart and "recsys-recommendation-ab-poller" in chart
    assert "automountServiceAccountToken: false" in chart
    assert "apps/v1" not in chart
    assert "kind: Role" not in chart and "kind: Ingress" not in chart
    assert "trigger, poll" in chart


def test_poll_label_claim_and_terminal_projection_are_idempotent(monkeypatch):
    monkeypatch.setenv("AB_TRIGGER_MODE", "poll")
    monkeypatch.setenv("AB_TRIGGER_SCOPE", "recommendation")
    monkeypatch.setenv("AB_LANGFUSE_PROMPT", "recsys-recommendation-ab")
    monkeypatch.setenv("AB_LANGFUSE_PARKING_VERSION", "1")
    versions = {
        1: {"name": "recsys-recommendation-ab", "version": 1,
            "prompt": PARKING_PROMPT_TEXT, "config": {
                "schema_version": 0, "scope": "recommendation", "kind": "ab-label-parking"},
            "labels": [PARKING_LABEL]},
        2: {"name": "recsys-recommendation-ab", "version": 2,
            "prompt": PROMPT_TEXT, "config": {}, "labels": [READY_LABEL]},
    }
    service = Trigger.__new__(Trigger)
    service.prompt = lambda name, version: deepcopy(versions[version])

    def move(name, version, labels):
        for label in labels:
            for value in versions.values():
                value["labels"] = [item for item in value["labels"] if item != label]
            versions[version]["labels"].append(label)

    service.move_labels = move
    recorded = []
    service._record_labels = lambda eid, status, candidate, parking, error=None: recorded.append(
        (eid, status, set(candidate["labels"]), set(parking["labels"])))
    row = {"experiment_id": "rec-" + "a" * 32, "prompt_name": "recsys-recommendation-ab",
           "prompt_version": 2, "status": "QUEUED"}
    service.claim_labels(row, deepcopy(versions[2]))
    service.claim_labels(row, deepcopy(versions[2]))
    assert ACTIVE_LABEL in versions[2]["labels"] and READY_LABEL not in versions[2]["labels"]
    assert READY_LABEL in versions[1]["labels"]

    class Result:
        def fetchall(self): return [{**row, "status": "COMPLETED"}]
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def execute(self, *args): return Result()
    class DB:
        def connect(self): return Connection()
    service.db = DB()
    service.sync_terminal_labels()
    assert "ab-done" in versions[2]["labels"]
    assert not ({READY_LABEL, ACTIVE_LABEL} & set(versions[2]["labels"]))
    assert {READY_LABEL, ACTIVE_LABEL} <= set(versions[1]["labels"])
