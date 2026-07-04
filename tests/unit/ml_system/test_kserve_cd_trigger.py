from __future__ import annotations

import json
import sys
import urllib.parse

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


def test_trigger_kserve_cd_posts_versioned_manifest_to_jenkins(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path / "promotion.json")
    status_path = tmp_path / "status.json"
    calls = []

    def fake_request(url, *, headers=None, data=None, opener=None, timeout=30):
        calls.append((url, headers or {}, data))
        if url.endswith("/crumbIssuer/api/json"):
            return 200, {}, json.dumps({"crumbRequestField": "Jenkins-Crumb", "crumb": "crumb-1"})
        if url.endswith("/buildWithParameters"):
            assert headers["Jenkins-Crumb"] == "crumb-1"
            payload = urllib.parse.parse_qs(data.decode("utf-8"))
            assert payload["PROMOTION_MANIFEST_URI"] == [
                "s3://recsys-model-store/promotions/bst/trial-001.json"
            ]
            assert payload["MODEL_VERSION"] == ["trial-001"]
            assert payload["METRIC_NAME"] == ["test_ndcg_at_10"]
            assert payload["TRIGGER_SOURCE"] == ["kubeflow-ray-training"]
            return 201, {"Location": "http://jenkins/queue/item/7/"}, ""
        raise AssertionError(f"Unexpected Jenkins URL: {url}")

    monkeypatch.setattr(trigger_kserve_cd, "request", fake_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "trigger_kserve_cd.py",
            "--manifest-path",
            str(manifest),
            "--score-threshold",
            "0",
            "--jenkins-url",
            "http://jenkins",
            "--job-name",
            "RecSys-KServe-Model-CD",
            "--jenkins-user",
            "admin",
            "--jenkins-token",
            "token",
            "--status-path",
            str(status_path),
            "--no-wait",
        ],
    )

    assert trigger_kserve_cd.main() == 0
    status = json.loads(status_path.read_text(encoding="utf-8"))

    assert status["triggered"] is True
    assert status["job_name"] == "RecSys-KServe-Model-CD"
    assert status["queue_url"] == "http://jenkins/queue/item/7/"
    assert len(calls) == 2


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
