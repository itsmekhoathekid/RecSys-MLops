import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.agentic.llm_ab_router.server import create_app
from apps.agentic.llm_ab_router.tickets import (
    bind_body,
    claims_for_case,
    sign,
    verify,
)
from jenkins.python.llm_agent_cd.external_cases import _body, run
from jenkins.python.llm_agent_cd.release import DEFAULT_POLICY, digest

SECRET = "test-ticket-secret-that-is-at-least-thirty-two-bytes"


def fixtures():
    return [
        {"id": f"case-{index:02d}", "prompt": f"prompt {index}", "expected": {}}
        for index in range(20)
    ]


def state():
    rows = fixtures()
    return {
        "phase": "AB",
        "experiment_id": "rec-test",
        "fixtures": rows,
        "fixture_checksum": digest(rows),
        "policy": DEFAULT_POLICY,
        "champion": {"scope": "recommendation"},
    }


def test_ticket_is_canonical_signed_expiring_and_body_bound():
    case = fixtures()[0]
    claims = claims_for_case("rec-test", case, digest(fixtures()), "nonce", now=100)
    ticket = sign(claims, SECRET)
    assert verify(ticket, SECRET, now=101) == claims
    assert (
        bind_body(claims, _body(claims["request_id"], case["prompt"])) == case["prompt"]
    )
    with pytest.raises(ValueError, match="expired"):
        verify(ticket, SECRET, now=claims["expires_at"] + 1)
    wrong = _body(claims["request_id"], "changed")
    with pytest.raises(ValueError, match="body"):
        bind_body(claims, wrong)
    forged = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")
    with pytest.raises(ValueError, match="signature"):
        verify(forged, SECRET, now=101)


class EdgeDB:
    def __init__(self):
        self.claimed = set()
        self.finished = []

    def migrate(self):
        pass

    def claim_external_ticket(self, claims):
        key = (claims["experiment_id"], claims["case_id"])
        if key in self.claimed:
            raise ValueError("replay")
        self.claimed.add(key)

    def finish_external_ticket(self, experiment_id, case_id, status, **fields):
        self.finished.append((experiment_id, case_id, status, fields))


class Store:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value, "etag"


def test_edge_strips_untrusted_headers_and_replay_never_reaches_router(monkeypatch):
    snapshot = state()
    case = snapshot["fixtures"][0]
    claims = claims_for_case(
        "rec-test", case, snapshot["fixture_checksum"], "nonce", now=int(time.time())
    )
    calls = []

    def upstream(request):
        calls.append(request)
        payload = json.loads(request.content)
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {}}
        )

    monkeypatch.setenv("AB_INTERNAL_TOKEN", "internal")
    monkeypatch.setenv("AB_SCOPE", "recommendation")
    monkeypatch.setenv("AB_CASE_TICKET_KEY", SECRET)
    monkeypatch.setenv("AB_ROUTER_URL", "http://private-router")
    monkeypatch.setenv("AB_EDGE_REVISION", "edge-test")
    db = EdgeDB()
    app = create_app(
        "edge",
        database=db,
        store=Store(snapshot),
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    headers = {
        "X-RecSys-AB-Ticket": sign(claims, SECRET),
        "X-RecSys-Source": "production",
        "X-RecSys-Release": "forged",
        "X-RecSys-Variant": "candidate",
        "X-User-Id": "attacker",
    }
    with TestClient(app) as client:
        response = client.post(
            "/a2a/recommendation-ab/v1",
            json=_body(claims["request_id"], case["prompt"]),
            headers=headers,
        )
        assert response.status_code == 200
        assert response.headers["x-recsys-ab-edge-revision"] == "edge-test"
        assert calls[0].headers["x-recsys-source"] == "synthetic"
        assert calls[0].headers["x-user-id"] == "llm-ab-suite"
        assert "x-recsys-release" not in calls[0].headers
        replay = client.post(
            "/a2a/recommendation-ab/v1",
            json=_body(claims["request_id"], case["prompt"]),
            headers=headers,
        )
        assert replay.status_code == 409
        assert len(calls) == 1
    assert db.finished[0][2] == "COMPLETED"


def test_edge_rejects_forged_ticket_and_consumes_body_mismatch(monkeypatch):
    snapshot = state()
    case = snapshot["fixtures"][0]
    claims = claims_for_case(
        "rec-test",
        case,
        snapshot["fixture_checksum"],
        "wrong-body-nonce",
        now=int(time.time()),
    )
    calls = []
    monkeypatch.setenv("AB_INTERNAL_TOKEN", "internal")
    monkeypatch.setenv("AB_SCOPE", "recommendation")
    monkeypatch.setenv("AB_CASE_TICKET_KEY", SECRET)
    monkeypatch.setenv("AB_ROUTER_URL", "http://private-router")
    monkeypatch.setenv("AB_EDGE_REVISION", "edge-test")
    db = EdgeDB()
    app = create_app(
        "edge",
        database=db,
        store=Store(snapshot),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(500)
            )
        ),
    )
    with TestClient(app) as client:
        forged = client.post(
            "/a2a/recommendation-ab/v1",
            json=_body(claims["request_id"], case["prompt"]),
            headers={"X-RecSys-AB-Ticket": sign(claims, SECRET + "different")},
        )
        assert forged.status_code == 403
        assert not db.claimed and not calls
        wrong = client.post(
            "/a2a/recommendation-ab/v1",
            json=_body(claims["request_id"], "changed prompt"),
            headers={"X-RecSys-AB-Ticket": sign(claims, SECRET)},
        )
        assert wrong.status_code == 403
        assert len(db.claimed) == 1 and not calls
        assert db.finished[-1][2] == "REJECTED"
        assert client.post("/", json={}).status_code == 404


class RunnerDB:
    def __init__(self):
        self.created = False
        self.intents = []

    def begin_external_suite(self, experiment_id, checksum, tickets):
        if self.created:
            return False
        self.created = True
        self.tickets = tickets
        return True

    def external_suite(self, experiment_id):
        if self.created:
            return {"run": {"status": "COMPLETED"}, "tickets": {"COMPLETED": 20}}
        return {"run": None, "tickets": {}}

    def mark_ticket_intent(self, experiment_id, case_id):
        self.intents.append(case_id)

    def mark_external_ambiguous(self, *args):
        raise AssertionError("successful transport must not be ambiguous")


def test_runner_sends_exactly_twenty_https_requests_and_never_replays(monkeypatch):
    monkeypatch.setenv("AB_CASE_TICKET_KEY", SECRET)
    monkeypatch.setenv("AB_EDGE_BASIC_USER", "jenkins")
    monkeypatch.setenv("AB_EDGE_BASIC_PASSWORD", "secret")
    monkeypatch.setenv(
        "AB_EXTERNAL_A2A_URL", "https://agents.example/a2a/recommendation-ab/v1"
    )
    requests = []

    def edge(request):
        requests.append(request)
        payload = json.loads(request.content)
        assert verify(request.headers["x-recsys-ab-ticket"], SECRET, now=101)
        return httpx.Response(
            200,
            headers={"X-RecSys-AB-Edge-Revision": "edge-v1"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {}},
        )

    db = RunnerDB()
    client = httpx.Client(transport=httpx.MockTransport(edge))
    heartbeats = []
    result = run(
        state(),
        database=db,
        client=client,
        clock=lambda: 0,
        sleeper=lambda _: None,
        wall_clock=lambda: 100,
        progress=lambda: heartbeats.append(True),
    )
    assert result["tickets"] == {"COMPLETED": 20}
    assert len(requests) == len(db.intents) == 20
    assert len(heartbeats) == 40
    run(
        state(),
        database=db,
        client=client,
        clock=lambda: 0,
        sleeper=lambda _: None,
        wall_clock=lambda: 100,
    )
    assert len(requests) == 20
