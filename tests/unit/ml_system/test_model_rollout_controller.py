from __future__ import annotations

import json
import os
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cli import model_rollout_controller as controller
from registry import model_promotion


ROOT = Path(__file__).resolve().parents[3]


@dataclass
class FakeVersion:
    version: str
    tags: dict[str, str] = field(default_factory=dict)


class FakeClient:
    def __init__(self, versions: list[FakeVersion]):
        self.versions = {version.version: version for version in versions}
        self.aliases: dict[str, str] = {}

    def search_model_versions(self, *_args, **_kwargs):
        return list(self.versions.values())

    def get_model_version(self, _name, version):
        return self.versions[str(version)]

    def set_model_version_tag(self, _name, version, key, value):
        self.versions[str(version)].tags[key] = str(value)

    def set_registered_model_alias(self, _name, alias, version):
        self.aliases[alias] = str(version)

    def get_model_version_by_alias(self, _name, alias):
        if alias not in self.aliases:
            raise KeyError(alias)
        return self.versions[self.aliases[alias]]

    def delete_registered_model_alias(self, _name, alias):
        self.aliases.pop(alias, None)


def candidate(version: str = "42", state: str = "test") -> FakeVersion:
    return FakeVersion(
        version=version,
        tags={
            "candidate": state,
            "model_version": f"bst-{version}",
            "metric_name": "test_ndcg_at_10",
            "metric_value": "0.42",
            "promotion_manifest_uri": f"s3://models/promotions/bst/bst-{version}.json",
        },
    )


def config() -> controller.RolloutConfig:
    return controller.RolloutConfig(
        control_manifest_uri="s3://models/promotions/bst/latest.json",
        stable_manifest_uri="s3://models/promotions/bst/latest.json",
        jenkins_url="http://jenkins",
        jenkins_user="admin",
        jenkins_token="token",
        jenkins_workspace="/var/jenkins_home/recsys-workspace",
    )


def test_pending_candidates_selects_latest_explicit_test_tag():
    client = FakeClient([candidate("3"), candidate("11"), candidate("12", "tested")])

    result = controller.pending_candidates(client, config())

    assert [version.version for version in result] == ["11", "3"]


def test_process_candidate_claims_then_marks_shadow_tested(monkeypatch):
    version = candidate()
    client = FakeClient([version])
    calls = []

    def fake_trigger(**kwargs):
        calls.append(kwargs)
        return {"build_number": 21, "build_result": "SUCCESS"}

    monkeypatch.setattr(controller, "trigger_jenkins_cd", fake_trigger)

    result = controller.process_candidate(client, config(), version)

    assert result["processed"] is True
    assert version.tags["candidate"] == "tested"
    assert version.tags["rollout_status"] == "shadow_ready"
    assert version.tags["rollout_build_number"] == "21"
    assert client.aliases["candidate"] == "42"
    params = calls[0]["params"]
    assert params["ROLLOUT_STAGE"] == "shadow-start"
    assert params["AB_CANDIDATE_WEIGHT_PERCENT"] == "0"
    assert params["CONTROL_MANIFEST_URI"].endswith("/latest.json")
    assert params["CANDIDATE_MANIFEST_URI"].endswith("/bst-42.json")


def test_process_candidate_failure_releases_alias_and_marks_failed(monkeypatch):
    version = candidate()
    client = FakeClient([version])

    def fail_trigger(**_kwargs):
        raise RuntimeError("jenkins failed")

    monkeypatch.setattr(controller, "trigger_jenkins_cd", fail_trigger)

    with pytest.raises(RuntimeError, match="jenkins failed"):
        controller.process_candidate(client, config(), version)

    assert version.tags["candidate"] == "failed"
    assert version.tags["rollout_status"] == "shadow_failed"
    assert "candidate" not in client.aliases


def test_promote_repoints_aliases_and_rollback_removes_candidate(monkeypatch):
    old = candidate("41", "promoted")
    new = candidate("42", "tested")
    client = FakeClient([old, new])
    client.aliases = {"champion": "41", "candidate": "42"}
    monkeypatch.setattr(
        controller,
        "trigger_jenkins_cd",
        lambda **_kwargs: {"build_number": 22, "build_result": "SUCCESS"},
    )

    controller.trigger_stage(client, config(), new, stage="promote")

    assert client.aliases == {"champion": "42", "previous": "41"}
    assert new.tags["candidate"] == "promoted"
    assert new.tags["rollout_status"] == "champion"

    client.aliases["candidate"] = "42"
    controller.trigger_stage(client, config(), new, stage="rollback")
    assert "candidate" not in client.aliases
    assert new.tags["candidate"] == "rolled_back"


def test_evaluate_reads_jenkins_decision_and_reconciles_rollback(monkeypatch):
    version = candidate("42", "tested")
    client = FakeClient([version])
    client.aliases["candidate"] = "42"
    monkeypatch.setattr(
        controller,
        "trigger_jenkins_cd",
        lambda **_kwargs: {
            "build_number": 23,
            "build_result": "SUCCESS",
            "build_url": "http://jenkins/job/RecSys-KServe-Model-CD/23/",
        },
    )
    monkeypatch.setattr(
        controller,
        "request",
        lambda *_args, **_kwargs: (
            200,
            {},
            'gate output {"decision": "rollback", "reasons": ["latency"]}',
        ),
    )

    controller.trigger_stage(client, config(), version, stage="evaluate", weight=25)

    assert version.tags["rollout_decision"] == "rollback"
    assert version.tags["rollout_status"] == "rolled_back"
    assert version.tags["candidate"] == "rolled_back"
    assert "candidate" not in client.aliases


def test_missing_candidate_manifest_is_quarantined_without_retry_loop():
    version = candidate()
    del version.tags["promotion_manifest_uri"]
    client = FakeClient([version])

    result = controller.process_candidate(client, config(), version)

    assert result == {
        "processed": False,
        "version": "42",
        "reason": "missing_promotion_manifest_uri",
    }
    assert version.tags["candidate"] == "invalid"
    assert version.tags["rollout_status"] == "manifest_missing"
    assert "promotion_manifest_uri" in version.tags["rollout_error"]
    assert controller.watch_once(client, config())["reason"] == "no_pending_or_active_candidate"


def test_watcher_auto_opens_first_ab_stage_after_shadow(monkeypatch):
    version = candidate("42", "tested")
    version.tags["rollout_status"] = "shadow_ready"
    client = FakeClient([version])
    calls = []

    def fake_stage(_client, _config, _version, *, stage, weight=0, gate_window=None):
        calls.append((stage, weight))
        return {"build_number": 24, "build_result": "SUCCESS"}

    monkeypatch.setattr(controller, "trigger_stage", fake_stage)

    result = controller.watch_once(client, config())

    assert result["action"] == "open_ab_10"
    assert calls == [("ab-start", 10)]


def test_watcher_waits_for_prometheus_samples_before_gate(monkeypatch):
    version = candidate("42", "tested")
    version.tags.update({"rollout_status": "ab_10", "rollout_stage_started_at": "100"})
    client = FakeClient([version])
    monkeypatch.setattr(
        controller,
        "stage_sample_counts",
        lambda *_args: {"candidate": 99.0, "control": 900.0, "elapsed_seconds": 60, "ready": False},
    )

    result = controller.watch_once(client, config())

    assert result["reason"] == "awaiting_prometheus_samples"
    assert result["weight"] == 10
    assert version.tags["rollout_candidate_samples"] == "99"
    assert version.tags["rollout_control_samples"] == "900"


def test_watcher_resumes_legacy_ab_state_with_fresh_sample_window(monkeypatch):
    version = candidate("42", "tested")
    version.tags["rollout_status"] = "hold_10"
    client = FakeClient([version])
    monkeypatch.setattr(controller.time, "time", lambda: 1234)

    result = controller.watch_once(client, config())

    assert result["action"] == "initialize_sample_window_10"
    assert version.tags["rollout_stage_started_at"] == "1234"
    assert version.tags["rollout_stage_weight"] == "10"
    assert version.tags["rollout_required_samples"] == "100"


def test_watcher_evaluates_only_after_both_variants_have_samples(monkeypatch):
    version = candidate("42", "tested")
    version.tags.update({"rollout_status": "ab_10", "rollout_stage_started_at": "100"})
    client = FakeClient([version])
    calls = []
    monkeypatch.setattr(
        controller,
        "stage_sample_counts",
        lambda *_args: {"candidate": 105.0, "control": 940.0, "elapsed_seconds": 60, "ready": True},
    )

    def fake_stage(_client, _config, _version, *, stage, weight=0, gate_window=None):
        calls.append((stage, weight, gate_window))
        return {"build_number": 25, "build_result": "SUCCESS"}

    monkeypatch.setattr(controller, "trigger_stage", fake_stage)

    result = controller.watch_once(client, config())

    assert result["action"] == "evaluate_10"
    assert result["gate_window"] == "60s"
    assert calls == [("evaluate", 10, "60s")]


@pytest.mark.parametrize(
    ("status", "expected_action", "expected_stage", "expected_weight"),
    [
        ("gate_passed_10", "increase_ab_25", "ab-step", 25),
        ("gate_passed_25", "increase_ab_50", "ab-step", 50),
        ("gate_passed_50", "promote_champion", "promote", 0),
    ],
)
def test_watcher_advances_passed_gates_to_champion(
    monkeypatch, status, expected_action, expected_stage, expected_weight
):
    version = candidate("42", "tested")
    version.tags["rollout_status"] = status
    client = FakeClient([version])
    calls = []

    def fake_stage(_client, _config, _version, *, stage, weight=0, gate_window=None):
        calls.append((stage, weight))
        return {"build_number": 26, "build_result": "SUCCESS"}

    monkeypatch.setattr(controller, "trigger_stage", fake_stage)

    result = controller.watch_once(client, config())

    assert result["action"] == expected_action
    assert calls == [(expected_stage, expected_weight)]


def test_rollout_watcher_helm_contract():
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "recsys-ci",
            str(ROOT / "infra/helm/recsys-ci"),
            "--set",
            "modelRolloutWatcher.enabled=true",
            "--set",
            "modelRolloutWatcher.image=registry/recsys-mlops-training:test",
        ],
        text=True,
    )

    assert "name: recsys-model-rollout-watcher" in rendered
    assert "registry/recsys-mlops-training:test" in rendered
    assert "model_rollout_controller.py" in rendered
    assert "name: CANDIDATE_PENDING_VALUE" in rendered
    assert "name: CONTROL_MANIFEST_URI" in rendered
    assert "name: JENKINS_TOKEN" in rendered
    assert "name: ROLLOUT_AUTO_PROGRESSIVE_ENABLED" in rendered
    assert "name: ROLLOUT_PROGRESSIVE_WEIGHTS" in rendered
    assert "name: ROLLOUT_STAGE_MIN_OBSERVATION_SECONDS" in rendered
    assert "name: recsys-jenkins-admin" in rendered


def test_champion_only_verifier_waits_for_api_rollout_before_requests():
    script = (ROOT / "jenkins/scripts/verify_champion_only.sh").read_text(encoding="utf-8")

    rollout_wait = script.index('kubectl rollout status "deployment/${deployment}"')
    api_request = script.index('urllib.request.Request("http://127.0.0.1:8080/recommendations"')
    assert rollout_wait < api_request


def test_locust_is_only_traffic_generator_for_autonomous_rollout():
    demo = (ROOT / "jenkins/scripts/model_rollout_demo.sh").read_text(encoding="utf-8")
    load = (ROOT / "jenkins/scripts/autonomous_rollout_locust.sh").read_text(encoding="utf-8")
    locustfile = (ROOT / "tests/load/locustfile_serving.py").read_text(encoding="utf-8")

    assert "progressive <mlflow-registry-version>" not in demo
    assert "progressive_rollout" not in demo
    assert 'locust_bin="${LOCUST_BIN' in load
    assert '"${locust_bin}" \\' in load
    assert "kill -KILL" in load
    assert 'status --version "${registry_version}"' in load
    assert '--stage' not in load
    assert "ROLLOUT_STAGE" not in load
    assert "_next_user_id()" in locustfile
    assert 'os.getenv("RECSYS_USER_ID", "").strip()' in locustfile


def test_locust_runner_accepts_legacy_duration_users_spawn_arguments():
    script = ROOT / "jenkins/scripts/autonomous_rollout_locust.sh"
    output = subprocess.check_output(
        ["bash", str(script), "15m", "10", "2"],
        text=True,
        env={**os.environ, "ROLLOUT_LOAD_PRINT_CONFIG": "1"},
    )

    assert "users=10" in output
    assert "spawn_rate=2" in output
    assert "max_duration=45m" in output
    assert "legacy_duration=15m" in output


def test_status_payload_is_demo_friendly():
    payload = controller.status_payload(candidate("42", "tested"), config())
    assert json.loads(json.dumps(payload))["candidate"] == "tested"
    assert payload["promotion_manifest_uri"].endswith("bst-42.json")


def test_new_mlflow_version_carries_versioned_manifest_tag(monkeypatch):
    captured = {}

    class RegistryClient:
        def get_registered_model(self, _name):
            return object()

        def create_model_version(self, **kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(version="17", status="READY")

    mlflow_module = types.ModuleType("mlflow")
    mlflow_module.set_tracking_uri = lambda _uri: None
    exceptions_module = types.ModuleType("mlflow.exceptions")
    exceptions_module.MlflowException = RuntimeError
    tracking_module = types.ModuleType("mlflow.tracking")
    tracking_module.MlflowClient = lambda tracking_uri=None: RegistryClient()
    monkeypatch.setitem(sys.modules, "mlflow", mlflow_module)
    monkeypatch.setitem(sys.modules, "mlflow.exceptions", exceptions_module)
    monkeypatch.setitem(sys.modules, "mlflow.tracking", tracking_module)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow")

    result = model_promotion.register_mlflow_model_version(
        registered_model_name="recsys_bst_ranker",
        source_uri="s3://artifacts/model",
        run_id="run-1",
        model_version="bst-17",
        metric_name="test_ndcg_at_10",
        metric_value=0.42,
        promotion_manifest_uri="s3://models/promotions/bst/bst-17.json",
    )

    assert result["mlflow_registered_model_version"] == "17"
    assert captured["tags"]["promotion_manifest_uri"] == "s3://models/promotions/bst/bst-17.json"
