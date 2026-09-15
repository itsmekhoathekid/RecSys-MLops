"""Real disposable PostgreSQL; fake Langfuse/Kubernetes/Jenkins, no inference."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import hmac
import io
import json
import os
import time

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from apps.agentic.llm_ab_router import trigger as module
from apps.agentic.llm_ab_router.database import Database
from tests.unit.jenkins.test_llm_workflow import bundle, champion

DSN = os.environ.get("LLM_AB_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable localhost PostgreSQL")


@pytest.fixture
def service(monkeypatch, bundle):
    from urllib.parse import urlparse
    assert urlparse(DSN).hostname in {"localhost", "127.0.0.1"}
    for key, value in {"AB_DATABASE_URL": DSN, "LANGFUSE_WEBHOOK_SECRET": "test-signing-key",
                       "LANGFUSE_PROJECT_ID": "test-project", "AB_LANGFUSE_PROMPT": "workflow",
                       "AB_DISPATCH_ENABLED": "true", "AB_STATE_URI": "s3://test/workflow/state.json"}.items():
        monkeypatch.setenv(key, value)
    obj = module.Trigger()
    obj.migrate()
    with obj.db.connect() as c:
        c.execute("TRUNCATE recsys_ab.trigger_events, recsys_ab.trigger_requests")
    config = {"schema_version": 1, "scope": "workflow", "baseline_workflow_release_id": bundle["release_id"],
              "global_generation": {**bundle["global_generation"], "temperature": "0.2"},
              "llm_release_ref": bundle["llm_version_id"], "experiment_type": "config_only", "policy_ref": "workflow-production"}
    obj.prompt = lambda name, version: {"name": name, "version": version, "prompt": module.PROMPT_TEXT,
                                       "labels": ["ab-ready"], "config": deepcopy(config)}
    jobs = []
    obj.ensure_job = lambda eid: jobs.append(eid)
    state = {"phase": "IDLE", "champion": bundle}

    class Store:
        def read(self): return deepcopy(state), "etag"
    monkeypatch.setattr(module, "StateStore", lambda _: Store())

    class S3:
        def __init__(self): self.objects = {}
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(self.objects.get(kwargs["Key"], json.dumps(bundle["llm"]).encode()))}
        def put_object(self, **kwargs):
            assert kwargs["IfNoneMatch"] == "*"
            self.objects[kwargs["Key"]] = kwargs["Body"]
    s3 = S3()
    monkeypatch.setattr(module, "s3_client", lambda: s3)
    yield obj, config, jobs, state
    obj.http.close()


def signed(eid="evt-1", version=1, timestamp=None, **extra):
    body = {"id": eid, "type": "prompt-version", "apiVersion": "v1", "action": "updated",
            "prompt": {"projectId": "test-project", "name": "workflow", "version": version, "labels": ["ab-ready"]}, **extra}
    raw = json.dumps(body).encode()
    at = str(int(time.time() if timestamp is None else timestamp))
    mac = hmac.new(b"test-signing-key", at.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return raw, "t=" + at + ",v1=" + mac


def test_concurrent_webhooks_persist_one_candidate_and_full_receipt(service):
    obj, config, jobs, _ = service
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda i: obj.accept(*signed("evt-" + str(i % 2))), range(8)))
    assert len({r["experiment_id"] for r in results}) == 1
    with obj.db.connect() as c:
        assert c.execute("SELECT count(*) n FROM recsys_ab.trigger_requests").fetchone()["n"] == 1
        assert c.execute("SELECT count(*) n FROM recsys_ab.trigger_events WHERE payload IS NOT NULL").fetchone()["n"] == 2
    row = obj.request(results[0]["experiment_id"])
    assert row["prompt_snapshot"]["config"] == config


def test_ack_is_durable_when_kubernetes_is_unavailable(service):
    obj, _, _, _ = service
    def unavailable(_): raise httpx.ConnectError("kube unavailable")
    obj.ensure_job = unavailable
    result = obj.accept(*signed())
    assert obj.request(result["experiment_id"])["status"] == "QUEUED"
    assert obj.accept(*signed())["duplicate"]


def test_recovery_creates_missing_candidate_job_before_dispatch(service):
    obj, _, jobs, _ = service
    eid = obj.accept(*signed())["experiment_id"]
    jobs.clear()
    obj.dispatch = lambda _: pytest.fail("must first recover deterministic candidate Job")
    obj.reconcile()
    assert jobs == [eid]


def test_collision_and_expired_new_event_fail_closed(service):
    obj, config, _, _ = service
    obj.accept(*signed())
    with pytest.raises(HTTPException) as e:
        obj.accept(*signed(version=2))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        obj.accept(*signed("expired", timestamp=time.time() - 400))
    assert e.value.status_code == 401
    config["global_generation"]["temperature"] = "0.3"
    with pytest.raises(HTTPException) as e:
        obj.accept(*signed("changed"))
    assert e.value.status_code == 409


def test_duplicate_job_processes_send_jenkins_once_and_never_retry_uncertain(service):
    obj, _, _, state = service
    eid = obj.accept(*signed())["experiment_id"]
    sent = []
    def jenkins(path, method="GET", **kwargs):
        if method == "POST":
            sent.append(kwargs["data"])
            raise httpx.ReadTimeout("response lost after Jenkins accepted")
        return httpx.Response(200, json={"builds": [], "items": []})
    obj.jenkins = jenkins
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(obj.dispatch, [eid, eid]))
    assert len(sent) == 1
    assert obj.request(eid)["status"] == "DISPATCHING"
    with obj.db.connect() as c:
        c.execute("UPDATE recsys_ab.trigger_requests SET submitted_at=now()-interval '2 minutes'")
    obj.dispatch(eid)
    obj.dispatch(eid)
    assert len(sent) == 1
    assert obj.request(eid)["status"] == "NEEDS_ATTENTION"


def test_uncertain_submission_reconciles_existing_build_without_post(service):
    obj, _, _, state = service
    eid = obj.accept(*signed())["experiment_id"]
    obj.update(eid, "DISPATCHING")
    state.update(experiment_id=eid, phase="COMPLETED")
    def jenkins(path, method="GET", **kwargs):
        assert method == "GET"
        build = {"number": 7, "url": "https://jenkins/job/workflow/7/", "building": False,
                 "actions": [{"parameters": [{"name": "EXPERIMENT_ID", "value": eid}]}]}
        return httpx.Response(200, json={"builds": [build]} if "/job/" in path else {"items": []})
    obj.jenkins = jenkins
    obj.dispatch(eid)
    assert obj.request(eid)["status"] == "COMPLETED"


def test_foreign_queue_entry_and_duplicate_builds_cannot_be_adopted(service):
    obj, _, _, _ = service
    eid = obj.accept(*signed())["experiment_id"]
    entry = {"id": 4, "task": {"name": "Final-ML"}, "actions": [{"parameters": [{"name": "EXPERIMENT_ID", "value": eid}]}]}
    obj.jenkins = lambda path, **kw: httpx.Response(200, json={"items": [entry], "builds": []})
    assert not obj.reconcile_build(obj.request(eid))
    obj.jenkins = lambda path, **kw: httpx.Response(200, json={"items": [], "builds": [entry, entry]})
    assert obj.reconcile_build(obj.request(eid))
    assert obj.request(eid)["status"] == "NEEDS_ATTENTION"


def test_recovery_projects_manual_rollback_without_redispatch(service):
    obj, _, jobs, state = service
    eid = obj.accept(*signed())["experiment_id"]
    url = "https://jenkins/job/workflow/7/"
    obj.update(eid, "NEEDS_ATTENTION", build_url=url)
    state.update(experiment_id=eid, phase="ROLLED_BACK")
    jobs.clear()
    obj.dispatch = lambda _: pytest.fail("manual rollback must never redispatch")
    def jenkins(path, method="GET", **kwargs):
        assert method == "GET"
        build = {"number": 7, "url": url, "building": False, "result": "ABORTED",
                 "actions": [{"parameters": [{"name": "EXPERIMENT_ID", "value": eid}]}]}
        return httpx.Response(200, json={"builds": [build]} if "/job/" in path else {"items": []})
    obj.jenkins = jenkins
    obj.reconcile()
    assert obj.request(eid)["status"] == "ROLLED_BACK"
    assert jobs == []


def test_failed_prestart_quarantine_reconciles_to_rejected(service):
    obj, config, _, state = service
    eid = obj.accept(*signed())["experiment_id"]
    _, candidate = obj._catalog_and_candidate(state, config)
    state["disabled"] = [candidate["release_id"]]
    url = "https://jenkins/job/workflow/44/"
    with obj.db.connect() as connection:
        connection.execute(
            """UPDATE recsys_ab.trigger_requests
               SET status='NEEDS_ATTENTION',build_url=%s,candidate=%s
               WHERE experiment_id=%s""",
            (url, module.Jsonb(candidate), eid),
        )

    def jenkins(path, method="GET", **kwargs):
        build = {
            "number": 44,
            "url": url,
            "building": False,
            "result": "FAILURE",
            "actions": [{"parameters": [{"name": "EXPERIMENT_ID", "value": eid}]}],
        }
        return httpx.Response(
            200, json={"builds": [build]} if "/job/" in path else {"items": []}
        )

    obj.jenkins = jenkins
    assert obj.reconcile_build(obj.request(eid))
    row = obj.request(eid)
    assert row["status"] == "REJECTED"
    assert row["reason"] == (
        "candidate release is quarantined; Jenkins rejected before experiment start"
    )


def test_recovery_leaves_uncertain_delivery_for_operator(service):
    obj, _, jobs, _ = service
    eid = obj.accept(*signed())["experiment_id"]
    obj.update(eid, "NEEDS_ATTENTION", reason="uncertain delivery")
    jobs.clear()
    obj.dispatch = lambda _: pytest.fail("must not resend uncertain delivery")
    obj.jenkins = lambda *args, **kwargs: pytest.fail("no known build to reconcile")
    obj.reconcile()
    assert obj.request(eid)["status"] == "NEEDS_ATTENTION"
    assert jobs == []


def test_stale_baseline_rejected_before_jenkins(service):
    obj, _, _, state = service
    eid = obj.accept(*signed())["experiment_id"]
    from tests.unit.jenkins.test_llm_workflow import candidate
    state["champion"] = candidate(state["champion"], config=True)
    obj.jenkins = lambda *a, **kw: pytest.fail("stale request must not reach Jenkins")
    obj.dispatch(eid)
    assert obj.request(eid)["status"] == "REJECTED"
    assert "STALE_BASELINE" in obj.request(eid)["reason"]


def test_quarantined_candidate_rejected_before_jenkins(service):
    obj, config, _, state = service
    eid = obj.accept(*signed())["experiment_id"]
    llm, candidate = obj._catalog_and_candidate(state, config)
    assert llm
    state["disabled"] = [candidate["release_id"]]
    obj.jenkins = lambda *a, **kw: pytest.fail(
        "quarantined candidate must not reach Jenkins"
    )
    obj.dispatch(eid)
    row = obj.request(eid)
    assert row["status"] == "REJECTED"
    assert row["reason"] == "candidate release is quarantined"


def test_webhook_stream_limit_and_status_auth(service, monkeypatch):
    obj, _, _, _ = service
    monkeypatch.setenv("AB_TRIGGER_STATUS_TOKEN", "status-test")
    with TestClient(module.create_app(obj)) as client:
        assert client.post("/webhooks/langfuse", content=b"x" * 262145).status_code == 413
        assert client.get("/experiments/unknown").status_code == 403
        assert client.get("/experiments/unknown", headers={"authorization": "Bearer status-test"}).status_code == 404


def test_dispatch_metrics_are_durable_gauges_without_sensitive_labels(service):
    obj, _, _, _ = service
    eid = obj.accept(*signed())["experiment_id"]
    obj.update(eid, "WAITING", reason="another experiment active")
    with obj.db.connect() as c:
        c.execute("UPDATE recsys_ab.trigger_requests SET created_at=now()-interval '2 minutes'")
    metrics = obj.metrics()
    assert '# TYPE recsys_workflow_dispatch_status gauge' in metrics
    queue = next(l for l in metrics.splitlines() if l.startswith("recsys_workflow_dispatch_queue_age_seconds{"))
    assert float(queue.rsplit(" ", 1)[1]) >= 120
    assert "test-signing-key" not in metrics and "reason=" not in metrics
    obj.update(eid, "COMPLETED")
    assert not any(l.startswith("recsys_workflow_dispatch_queue_age_seconds{") for l in obj.metrics().splitlines())
