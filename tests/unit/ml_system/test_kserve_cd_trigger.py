from __future__ import annotations

import json
import sys

from cli import trigger_kserve_cd


def _manifest(path, metric_value=0.31):
    path.write_text(
        json.dumps(
            {
                "model_name": "bst",
                "model_version": "trial-001",
                "metric_name": "test_ndcg_at_10",
                "metric_value": metric_value,
                "promotion_manifest_uri": "s3://recsys-model-store/promotions/bst/trial-001.json",
                "triton_storage_uri": "s3://recsys-model-store/triton/bst/trial-001",
                "mlflow_registered_model_name": "recsys_bst_ranker",
                "mlflow_registered_model_version": "17",
            }
        ),
        encoding="utf-8",
    )
    return path


def test_trigger_kserve_cd_skips_when_score_is_below_threshold(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path / "promotion.json", metric_value=-0.1)
    status_path = tmp_path / "status.json"

    def fail_request(*args, **kwargs):
        raise AssertionError("Jenkins should not be called when the promotion score is below threshold")

    monkeypatch.setattr(trigger_kserve_cd, "request", fail_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trigger_kserve_cd.py",
            "--manifest-path",
            str(manifest),
            "--score-threshold",
            "0",
            "--status-path",
            str(status_path),
        ],
    )

    assert trigger_kserve_cd.main() == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))

    assert status["triggered"] is False
    assert status["reason"] == "score_below_threshold"
    assert status["metric_value"] == -0.1


def test_existing_champion_waits_for_candidate_tag_without_calling_jenkins(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path / "promotion.json")
    status_path = tmp_path / "status.json"
    registry_calls = []

    monkeypatch.setattr(trigger_kserve_cd, "manifest_exists", lambda _uri: True)
    monkeypatch.setattr(
        trigger_kserve_cd,
        "set_registry_rollout_state",
        lambda payload, **kwargs: registry_calls.append((payload, kwargs)) or ("recsys_bst_ranker", "17"),
    )
    monkeypatch.setattr(
        trigger_kserve_cd,
        "trigger_jenkins_cd",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Jenkins must wait for candidate=test")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trigger_kserve_cd.py",
            "--manifest-path",
            str(manifest),
            "--score-threshold",
            "0",
            "--status-path",
            str(status_path),
        ],
    )

    assert trigger_kserve_cd.main() == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))

    assert status["triggered"] is False
    assert status["reason"] == "champion_exists_waiting_for_candidate_tag"
    assert status["next_action"] == "set candidate=test on MLflow registry version 17"
    assert registry_calls[0][1] == {"rollout_status": "awaiting_candidate_selection"}


def test_cold_start_bootstraps_jenkins_then_publishes_champion(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path / "promotion.json")
    status_path = tmp_path / "status.json"
    calls = []

    monkeypatch.setattr(trigger_kserve_cd, "manifest_exists", lambda _uri: False)
    monkeypatch.setattr(
        trigger_kserve_cd,
        "trigger_jenkins_cd",
        lambda **kwargs: calls.append(("jenkins", kwargs))
        or {"triggered": True, "build_number": 9, "build_result": "SUCCESS"},
    )
    monkeypatch.setattr(
        trigger_kserve_cd,
        "publish_stable_manifest",
        lambda payload, uri: calls.append(("publish", uri)) or payload,
    )
    monkeypatch.setattr(
        trigger_kserve_cd,
        "set_registry_rollout_state",
        lambda payload, **kwargs: calls.append(("registry", kwargs)) or ("recsys_bst_ranker", "17"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trigger_kserve_cd.py",
            "--manifest-path",
            str(manifest),
            "--status-path",
            str(status_path),
            "--stable-manifest-uri",
            "s3://recsys-model-store/promotions/bst/latest.json",
        ],
    )

    assert trigger_kserve_cd.main() == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))

    assert status["triggered"] is True
    assert status["reason"] == "initial_champion_bootstrap"
    assert [call[0] for call in calls] == ["jenkins", "publish", "registry"]
    params = calls[0][1]["params"]
    assert params["ROLLOUT_STAGE"] == "deploy"
    assert params["PROMOTION_MANIFEST_URI"].endswith("trial-001.json")
    assert params["AB_CANDIDATE_WEIGHT_PERCENT"] == "0"
    assert params["TRIGGER_SOURCE"] == "kubeflow-cold-start-bootstrap"
    assert calls[2][1] == {"rollout_status": "champion", "alias": "champion"}


def test_cold_start_rejects_no_wait_mode(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path / "promotion.json")
    monkeypatch.setattr(trigger_kserve_cd, "manifest_exists", lambda _uri: False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["trigger_kserve_cd.py", "--manifest-path", str(manifest), "--no-wait"],
    )

    try:
        trigger_kserve_cd.main()
    except ValueError as exc:
        assert "requires --wait" in str(exc)
    else:
        raise AssertionError("Cold-start bootstrap must reject asynchronous Jenkins handoff")


def test_publish_stable_manifest_preserves_immutable_source_and_sets_stable_target(tmp_path):
    source = {
        "model_version": "trial-001",
        "triton_storage_uri": "s3://models/triton/bst/trial-001",
        "promotion_manifest_uri": "s3://models/promotions/bst/trial-001.json",
    }
    target = tmp_path / "latest.json"

    stable = trigger_kserve_cd.publish_stable_manifest(source, str(target))

    assert stable["promotion_manifest_uri"] == str(target)
    assert stable["serving_storage_uri"] == "s3://recsys-model-store/triton/bst/latest"
    assert stable["triton_storage_uri"] == source["triton_storage_uri"]
    assert json.loads(target.read_text(encoding="utf-8")) == stable


def test_trigger_kserve_cd_waits_for_successful_build(monkeypatch):
    def fake_request(url, *, headers=None, data=None, opener=None, timeout=30):
        if url.endswith("/crumbIssuer/api/json"):
            return 200, {}, json.dumps({"crumbRequestField": "Jenkins-Crumb", "crumb": "crumb-1"})
        if url.endswith("/buildWithParameters"):
            return 201, {"Location": "http://jenkins/queue/item/7/"}, ""
        if url.endswith("/queue/item/7/api/json"):
            return 200, {}, json.dumps({"executable": {"url": "http://jenkins/job/RecSys-KServe-Model-CD/9/"}})
        if url.endswith("/job/RecSys-KServe-Model-CD/9/api/json"):
            return 200, {}, json.dumps({"number": 9, "building": False, "result": "SUCCESS"})
        raise AssertionError(f"Unexpected Jenkins URL: {url}")

    monkeypatch.setattr(trigger_kserve_cd, "request", fake_request)
    monkeypatch.setattr(trigger_kserve_cd.time, "sleep", lambda _: None)

    result = trigger_kserve_cd.trigger_jenkins_cd(
        jenkins_url="http://jenkins",
        job_name="RecSys-KServe-Model-CD",
        params={"PROMOTION_MANIFEST_URI": "s3://bucket/promotions/bst/trial-001.json"},
        user="admin",
        token="token",
        wait=True,
        poll_interval_seconds=1,
        timeout_seconds=30,
    )

    assert result["build_number"] == 9
    assert result["build_result"] == "SUCCESS"
