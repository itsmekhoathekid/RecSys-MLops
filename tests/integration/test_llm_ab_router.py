"""Real PostgreSQL, fake upstream A2A. Never uses production or generates tokens."""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.agentic.llm_ab_router.database import Database
from apps.agentic.llm_ab_router.server import create_app
from jenkins.python.llm_agent_cd.release import DEFAULT_POLICY, digest
from tests.unit.jenkins.test_llm_agent_cd import task_fixture
from tests.unit.jenkins.test_llm_workflow import bundle, champion, candidate

DSN = os.environ.get("LLM_AB_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="set LLM_AB_TEST_DATABASE_URL to disposable localhost PostgreSQL"
)


@pytest.fixture
def runtime(monkeypatch):
    assert "127.0.0.1" in DSN or "localhost" in DSN, (
        "integration tests must never truncate production"
    )
    db = Database(DSN)
    db.migrate()
    with db.connect() as c:
        c.execute(
            "TRUNCATE recsys_ab.invocations, recsys_ab.sessions, recsys_ab.heartbeat"
        )
    monkeypatch.setenv("AB_INTERNAL_TOKEN", "test-token")
    monkeypatch.setenv("HOSTNAME", "recsys-ab-router-test")
    monkeypatch.setenv("AB_SCOPE", "recommendation")
    monkeypatch.setenv("AB_GATEWAY_URL", "http://gateway")
    monkeypatch.setenv("AB_LIVE_TEST_TOKEN", "live-test-token")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    cases = json.loads(
        (Path(__file__).resolve().parents[2] / "configs/llm-ab/cases.json").read_text()
    )
    state = {
        "phase": "AB",
        "experiment_id": "test",
        "disabled": [],
        "fixtures": cases,
        "policy": DEFAULT_POLICY,
        "champion": {"release_id": "A"},
        "pending": {"release_id": "B"},
        "baseline": {"release_id": "A"},
        "releases": {
            "A": {"release_id": "A", "config_id": "ca", "llm_version_id": "la"}
        },
    }
    calls = []

    class Store:
        def read(self):
            return deepcopy(state), "etag"

    def upstream(request):
        calls.append((request.method, request.url.path, dict(request.headers)))
        if request.url.path in {"/allocate", "/identity"}:
            return httpx.Response(200, json={"release_id": "A"})
        time.sleep(0.03)  # Force overlap for duplicate request tests.
        body, _ = task_fixture()
        payload = json.loads(request.content)
        context = payload["params"]["message"]["contextId"]
        prompt = payload["params"]["message"]["parts"][0].get("text")
        if prompt == "Recommend three items. I have not provided a user ID.":
            body = {
                "result": {
                    "task": {
                        "id": "missing-user-task",
                        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
                        "history": [deepcopy(payload["params"]["message"])],
                        "artifacts": [
                            {
                                "parts": [
                                    {
                                        "metadata": {"adk_type": "function_call"},
                                        "data": {
                                            "id": "ask-1",
                                            "name": "ask_user",
                                            "args": {"questions": [{
                                                "question": "Please provide your user ID."
                                            }]},
                                        },
                                    },
                                    {
                                        "metadata": {"adk_type": "function_response"},
                                        "data": {
                                            "id": "ask-1",
                                            "name": "ask_user",
                                            "response": {"status": "pending"},
                                        },
                                    },
                                ]
                            }
                        ],
                    }
                }
            }
        body["result"]["task"]["contextId"] = context
        return httpx.Response(200, json=body)

    http = httpx.Client(transport=httpx.MockTransport(upstream))
    app = create_app("router", database=db, store=Store(), client=http)
    with TestClient(app) as client:
        yield client, db, state, calls


def message(mid="message-1", context="session-1"):
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "method": "SendMessage",
        "params": {
            "message": {
                "messageId": mid,
                "contextId": context,
                "role": "ROLE_USER",
                "parts": [{"text": "recommend"}],
            }
        },
    }


HEADERS = {"authorization": "Bearer test-token", "x-user-id": "user-1"}


def test_concurrent_duplicates_execute_once_and_session_survives(runtime):
    client, db, state, calls = runtime
    with ThreadPoolExecutor(2) as pool:
        responses = list(
            pool.map(
                lambda _: client.post("/", json=message(), headers=HEADERS), range(2)
            )
        )
    assert all(r.status_code == 200 for r in responses)
    assert sum(method == "POST" for method, _, _ in calls) == 1
    assert responses[0].json() == responses[1].json()
    assert responses[0].json()["result"]["task"]["contextId"] == "session-1"
    assert db.result(digest(["user-1", "message-1"]))["verdict"] == "PASS"
    client.post("/", json=message("message-2"), headers=HEADERS)
    assert sum(path == "/allocate" for _, path, _ in calls) == 1


def test_internal_headers_cannot_select_release_and_unknown_task_is_private(runtime):
    client, db, state, calls = runtime
    assert client.post("/", json=message()).status_code == 403
    response = client.post(
        "/", json=message(), headers={**HEADERS, "x-recsys-release": "B"}
    )
    assert response.status_code == 200
    assert calls[-1][2]["x-recsys-release"] == "A"
    polling = {
        "jsonrpc": "2.0",
        "id": "poll",
        "method": "GetTask",
        "params": {"id": "task-1"},
    }
    assert (
        client.post(
            "/", json=polling, headers={**HEADERS, "x-user-id": "other"}
        ).status_code
        == 404
    )
    assert client.post("/", json=polling, headers=HEADERS).status_code == 200


def test_quarantine_never_replays_on_champion(runtime):
    client, db, state, calls = runtime
    client.post("/", json=message(), headers=HEADERS)
    count = len(calls)
    state["disabled"] = ["A"]
    result = client.post("/", json=message("message-2"), headers=HEADERS)
    assert result.status_code == 409
    assert len(calls) == count


def test_session_cannot_change_trusted_traffic_source(runtime):
    client, db, state, calls = runtime
    assert client.post("/", json=message(), headers=HEADERS).status_code == 200
    before = len(calls)
    response = client.post(
        "/",
        json=message("live-message", "session-1"),
        headers={
            **HEADERS,
            "x-recsys-source": "live_test",
            "x-recsys-test-token": "live-test-token",
        },
    )
    assert response.status_code == 409
    assert len(calls) == before


def test_ambiguous_claim_is_not_reissued(runtime):
    client, db, state, calls = runtime
    client.post("/", json=message(), headers=HEADERS)
    with db.connect() as c:
        c.execute(
            "UPDATE recsys_ab.invocations SET response=NULL,result=NULL,finished_at=NULL"
        )
    count = len(calls)
    assert client.post("/", json=message(), headers=HEADERS).status_code == 409
    assert len(calls) == count


def test_production_observation_and_metrics_share_durable_evidence(runtime):
    client, db, state, calls = runtime
    before = time.time() - 1
    client.post("/", json=message(), headers=HEADERS)
    observation = db.observe(state, before, time.time() + 1)
    assert observation["healthy"]
    assert observation["champion"]["count"] == 1
    assert observation["champion"]["unknown"] == 0
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "recsys_ab_count{" in metrics.text
    assert 'config_id="ca"' in metrics.text


def test_unregistered_synthetic_case_rejected(runtime):
    client, db, state, calls = runtime
    result = client.post(
        "/", json=message(), headers={**HEADERS, "x-recsys-source": "synthetic"}
    )
    assert result.status_code == 403
    assert not calls


def test_recommendation_live_test_uses_frozen_missing_user_contract(runtime):
    client, db, state, calls = runtime
    fixture = next(case for case in state["fixtures"] if case["id"] == "missing-user-1")
    payload = message("missing-user-live", "missing-user-live")
    payload["params"]["message"]["parts"] = [{"text": fixture["prompt"]}]
    response = client.post(
        "/",
        json=payload,
        headers={
            **HEADERS,
            "x-recsys-source": "live_test",
            "x-recsys-test-token": "live-test-token",
        },
    )
    assert response.status_code == 200
    result = db.result(digest(["user-1", "missing-user-live"]))
    assert result["verdict"] == "PASS"
    assert result["reason"] == "native ask_user clarification without dependency execution"


def test_workflow_sql_snapshot_is_replica_safe_and_sessions_count_once(runtime, bundle):
    from apps.agentic.llm_ab_router.workflow_metrics import render
    client, db, state, calls = runtime
    start = time.time() - 1
    client.post("/", json=message(), headers=HEADERS)
    state["phase"] = "VERIFY"
    client.post("/", json=message("second-turn"), headers=HEADERS)
    with db.connect() as c:
        c.execute("UPDATE recsys_ab.invocations SET release_id=%s", (bundle["release_id"],))
    workflow_state = {"phase": "VERIFY", "experiment_id": "test", "experiment_started": start,
                      "champion": bundle, "baseline": bundle, "pending": candidate(bundle, True)}
    first, second = render(db, workflow_state), render(db, workflow_state)
    assert first == second
    series = [l.rsplit(" ", 1)[0] for l in first.splitlines() if not l.startswith("#")]
    assert len(series) == len(set(series))
    sessions = [float(l.rsplit(" ", 1)[1]) for l in first.splitlines() if l.startswith("recsys_workflow_sessions_total{")]
    completed = [float(l.rsplit(" ", 1)[1]) for l in first.splitlines() if l.startswith("recsys_workflow_completed_total{")]
    assert sum(sessions) == 1 and sum(completed) == 2


def test_terminal_session_retirement_is_conservative_and_source_aware(runtime):
    client, db, state, calls = runtime
    releases = {
        "production": "prod",
        "synthetic": "synthetic",
        "disabled": "disabled",
        "unknown": "unknown",
        "unfinished": "unfinished",
    }
    with db.connect() as c, c.transaction():
        for key, release_id in releases.items():
            c.execute(
                """INSERT INTO recsys_ab.sessions(
                     session_key,release_id,backend_context)
                   VALUES (%s,%s,%s)""",
                ("cleanup-" + key, release_id, "context-" + key),
            )
        for key, source, finished in (
            ("production", "production", True),
            ("synthetic", "synthetic", True),
            ("disabled", "production", True),
            ("unfinished", "live_test", False),
        ):
            c.execute(
                """INSERT INTO recsys_ab.invocations(
                     request_key,session_key,experiment_id,source,release_id,finished_at)
                   VALUES (%s,%s,'cleanup-test',%s,%s,
                     CASE WHEN %s THEN now() ELSE NULL END)""",
                (
                    "request-" + key,
                    "cleanup-" + key,
                    source,
                    releases[key],
                    finished,
                ),
            )

    report = db.retire_terminal_sessions([releases["disabled"]])

    assert report["closed"] == 2
    assert report["closed_total"] >= 2
    assert report["closed_by_reason"] == {
        "release_quarantined": 1,
        "terminal_test_session": 1,
    }
    assert set(report["protected_release_ids"]) == {
        releases["production"],
        releases["unknown"],
        releases["unfinished"],
    }
    assert report["unfinished_invocations"] == 1
    with db.connect() as c:
        rows = c.execute(
            """SELECT session_key,source,closed_at,close_reason
               FROM recsys_ab.sessions WHERE session_key LIKE 'cleanup-%%'"""
        ).fetchall()
    indexed = {row["session_key"]: row for row in rows}
    assert indexed["cleanup-production"]["source"] == "production"
    assert indexed["cleanup-production"]["closed_at"] is None
    assert indexed["cleanup-synthetic"]["close_reason"] == "terminal_test_session"
    assert indexed["cleanup-disabled"]["close_reason"] == "release_quarantined"
    assert indexed["cleanup-unknown"]["closed_at"] is None
    assert indexed["cleanup-unfinished"]["closed_at"] is None
    repeated = db.retire_terminal_sessions([])
    assert repeated["closed"] == 0
    assert repeated["closed_total"] == report["closed_total"]
    assert set(repeated["protected_release_ids"]) == {
        releases["production"],
        releases["unknown"],
        releases["unfinished"],
    }


def test_recreated_router_uses_persisted_assignment_and_cache(runtime):
    client, db, state, calls = runtime
    client.post("/", json=message(), headers=HEADERS)
    fresh_calls = []

    class Store:
        def read(self):
            return deepcopy(state), "etag"

    def upstream(request):
        fresh_calls.append(request.url.path)
        assert request.url.path != "/allocate", "restart must preserve assignment"
        body, _ = task_fixture()
        return httpx.Response(200, json=body)

    app = create_app(
        "router",
        database=Database(db.dsn),
        store=Store(),
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as fresh:
        duplicate = message()
        duplicate["id"] = "new-envelope-id"
        response = fresh.post("/", json=duplicate, headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["id"] == "new-envelope-id"
        assert not fresh_calls
        altered = message()
        altered["params"]["message"]["parts"] = [{"text": "different request"}]
        assert fresh.post("/", json=altered, headers=HEADERS).status_code == 409
        assert (
            fresh.post("/", json=message("message-2"), headers=HEADERS).status_code
            == 200
        )
        assert fresh_calls == ["/"]


def test_workflow_and_legacy_same_identity_do_not_share_assignment_or_task(runtime, monkeypatch):
    legacy, db, state, legacy_calls = runtime
    old = legacy.post("/", json=message(), headers=HEADERS)
    task_id = old.json()["result"]["task"]["id"]
    monkeypatch.setenv("AB_SCOPE", "workflow")
    workflow_state = deepcopy(state)
    workflow_state["releases"] = {"B": {"release_id": "B"}}
    workflow_state["champion"] = workflow_state["baseline"] = {"release_id": "B"}
    calls = []

    class Store:
        def read(self): return deepcopy(workflow_state), "workflow-etag"
    def upstream(request):
        calls.append(request.url.path)
        if request.url.path == "/allocate": return httpx.Response(200, json={"release_id": "B"})
        assert request.headers["x-recsys-release"] == "B"
        body, _ = task_fixture()
        return httpx.Response(200, json=body)
    app = create_app("router", database=db, store=Store(), client=httpx.Client(transport=httpx.MockTransport(upstream)))
    with TestClient(app) as workflow:
        poll = {"jsonrpc": "2.0", "id": "poll", "method": "GetTask", "params": {"id": task_id}}
        assert workflow.post("/", json=poll, headers=HEADERS).status_code == 404
        result = workflow.post("/", json=message(), headers=HEADERS)
        assert result.status_code == 200
        assert calls == ["/allocate", "/"]
        assert workflow.post("/", json=message(), headers=HEADERS).status_code == 200
        assert calls == ["/allocate", "/"]
        assert legacy.post("/", json=poll, headers=HEADERS).status_code == 200
    with db.connect() as c:
        assert c.execute("SELECT count(*) n FROM recsys_ab.sessions").fetchone()["n"] == 2
        assert c.execute("SELECT count(*) n FROM recsys_ab.invocations").fetchone()["n"] == 2
