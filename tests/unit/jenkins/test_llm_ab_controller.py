import json
import hashlib
import time
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from apps.agentic.llm_ab_router.controller import (
    RecommendationController,
    epoch_seconds,
    gate_decision,
    json_evidence,
)
from apps.agentic.llm_ab_router.server import create_app
from apps.agentic.llm_ab_router.tickets import claims_for_live, sign, verify
from jenkins.python.llm_agent_cd.external_cases import _body
from jenkins.python.llm_agent_cd.llm_ab_traffic import (
    _ready_to_drain,
    _send_live,
    job_manifest,
)
from jenkins.python.llm_agent_cd.release import digest


SECRET = "test-ticket-secret-that-is-at-least-thirty-two-bytes"


def test_controller_decisions_cover_pass_fail_hold_and_timeout():
    assert gate_decision("CANARY", "HOLD", 600, 600, 3600) == (None, None)
    assert gate_decision("CANARY", "PASS", 599, 600, 3600) == (None, None)
    assert gate_decision("CANARY", "PASS", 600, 600, 3600) == ("route", 50)
    assert gate_decision("AB", "PASS", 600, 600, 3600) == ("route", 100)
    assert gate_decision("VERIFY", "PASS", 600, 600, 3600) == ("promote", None)
    assert gate_decision("AB", "FAIL", 1, 600, 3600) == ("rollback", None)
    assert gate_decision("AB", "HOLD", 3600, 600, 3600) == ("rollback", None)
    assert gate_decision("AB", "PASS", 600, 600, 3600, pending_live=True) == (None, None)


def test_traffic_runner_drains_only_after_controller_pass_and_full_window():
    state = {
        "stage_started": 1000,
        "policy": {"window_seconds": 600},
        "gate": {"verdict": "PASS"},
    }
    assert not _ready_to_drain(state, now=1599)
    assert _ready_to_drain(state, now=1600)
    state["gate"] = {"verdict": "HOLD"}
    assert not _ready_to_drain(state, now=2000)


def test_action_key_changes_with_state_revision():
    base = ("rec-" + "a" * 32, "route", 50, "CANARY")
    first = RecommendationController.action_key(*base, '"etag-a"')
    assert first == RecommendationController.action_key(*base, '"etag-a"')
    assert first != RecommendationController.action_key(*base, '"etag-b"')


def test_database_evidence_datetimes_are_normalized_for_cas_state():
    value = {"run": {"heartbeat_at": datetime(2026, 9, 15, tzinfo=timezone.utc)}}

    normalized = json_evidence(value)

    assert normalized == {"run": {"heartbeat_at": 1789430400.0}}
    assert epoch_seconds(normalized["run"]["heartbeat_at"]) == 1789430400.0
    assert epoch_seconds(value["run"]["heartbeat_at"]) == 1789430400.0
    json.dumps(normalized, allow_nan=False)


def test_failed_prepare_before_state_ownership_rejects_without_redispatch():
    updates = []

    class Trigger:
        db = None

        def request(self, experiment_id):
            return {"experiment_id": experiment_id}

        def update(self, experiment_id, status, reason=None):
            updates.append((experiment_id, status, reason))

    class DB:
        def latest_controller_action(self, _experiment_id):
            return {
                "status": "FAILED", "action": "prepare",
                "reason": "Jenkins result FAILURE",
            }

    class State:
        def read(self):
            return {"experiment_id": "previous", "phase": "ROLLED_BACK"}, '"etag"'

    controller = RecommendationController.__new__(RecommendationController)
    controller.trigger = Trigger()
    controller.db = DB()
    controller.store = State()

    result = controller.tick("rec-" + "f" * 32)

    assert result["status"] == "REJECTED"
    assert updates[0][1] == "REJECTED"


def test_traffic_job_is_single_bounded_tokenless_public_runner():
    experiment = "rec-" + "b" * 32
    image = "registry.example/router@sha256:" + "c" * 64
    job = job_manifest(
        experiment, image, "kagent",
        node_selector={"recsys.ai/pool": "ml-system"},
        tolerations=[{"key": "recsys.ai/workload", "operator": "Exists"}],
    )
    assert job["metadata"]["name"] == "ab-traffic-" + "b" * 32
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 14400
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    command = " ".join(pod["containers"][0]["command"])
    assert "llm_ab_traffic run" in command
    assert "jenkins" not in json.dumps(job).lower().replace(
        "jenkins.python.llm_agent_cd.llm_ab_traffic", ""
    )


class LiveDB:
    def __init__(self):
        self.claims = []
        self.finished = []

    def migrate(self):
        pass

    def claim_live_ticket(self, claims):
        if self.claims:
            raise ValueError("replay")
        self.claims.append(claims)

    def finish_live_ticket(self, request_id, status, **values):
        self.finished.append((request_id, status, values))


class Store:
    def read(self):
        return {"phase": "CANARY", "experiment_id": "rec-live"}, '"etag"'


def test_live_ticket_source_is_server_authenticated(monkeypatch):
    request_id = digest(["rec-live", "live", 1])
    prompt = "recommend for user 42"
    claims = claims_for_live(
        "rec-live", "CANARY", request_id, prompt, "nonce", now=int(time.time())
    )
    assert verify(sign(claims, SECRET), SECRET)["traffic_kind"] == "live_test"
    calls = []

    def upstream(request):
        calls.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

    monkeypatch.setenv("AB_INTERNAL_TOKEN", "internal")
    monkeypatch.setenv("AB_LIVE_TEST_TOKEN", "trusted-live")
    monkeypatch.setenv("AB_SCOPE", "recommendation")
    monkeypatch.setenv("AB_CASE_TICKET_KEY", SECRET)
    monkeypatch.setenv("AB_ROUTER_URL", "http://private-router")
    monkeypatch.setenv("AB_EDGE_REVISION", "edge-test")
    app = create_app(
        "edge", database=LiveDB(), store=Store(),
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/a2a/recommendation-ab/v1", json=_body(request_id, prompt),
            headers={
                "X-RecSys-AB-Ticket": sign(claims, SECRET),
                "X-RecSys-Source": "production",
                "X-RecSys-Variant": "candidate",
            },
        )
    assert response.status_code == 200
    assert calls[0].headers["x-recsys-source"] == "live_test"
    assert calls[0].headers["x-recsys-test-token"] == "trusted-live"
    assert calls[0].headers["x-user-id"] == "llm-ab-live"
    assert "x-recsys-variant" not in calls[0].headers


def test_live_traffic_uses_only_the_frozen_fixture_suite(monkeypatch):
    fixtures = [
        {"id": f"case-{index}", "prompt": f"fixture prompt {index}"}
        for index in range(20)
    ]
    state = {
        "experiment_id": "rec-live",
        "phase": "CANARY",
        "fixtures": fixtures,
        "fixture_checksum": digest(fixtures),
    }

    class DB:
        def traffic_run(self, _experiment_id):
            return {"live_submitted": 0}

        def issue_live_ticket(self, claims):
            self.claims = claims
            return claims

        def mark_live_ambiguous(self, *_args):
            return "AMBIGUOUS"

    captured = {}

    def upstream(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"X-RecSys-AB-Edge-Revision": "edge-test"},
            json={"jsonrpc": "2.0", "id": captured["body"]["id"]},
        )

    database = DB()
    monkeypatch.setenv("AB_CASE_TICKET_KEY", SECRET)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    client = httpx.Client(transport=httpx.MockTransport(upstream))

    _send_live(state, database, client)

    assert captured["body"]["params"]["message"]["parts"][0]["text"] == fixtures[0]["prompt"]
    assert database.claims["prompt_sha256"] == hashlib.sha256(
        fixtures[0]["prompt"].encode("utf-8")
    ).hexdigest()


def test_jenkins_is_executor_only_and_workflow_pipeline_is_unchanged():
    recommendation = open("jenkins/LLMAgentCD.Jenkinsfile").read()
    workflow = open("jenkins/LLMWorkflowCD.Jenkinsfile").read()
    assert "while (" not in recommendation
    assert "sleep(time:" not in recommendation
    assert "recommendation_action" in recommendation
    for name in ("TARGET_WEIGHT", "CANDIDATE_MANIFEST", "EXPERIMENT_TYPE", "REASON"):
        assert '"${' + name + ':-}"' in recommendation
    assert "post {" in recommendation and " cleanup" not in recommendation.split("post {", 1)[1]
    assert "resume" in workflow
