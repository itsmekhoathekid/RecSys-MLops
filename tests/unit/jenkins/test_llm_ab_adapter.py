import json
import time
from contextlib import contextmanager

import httpx
from fastapi.testclient import TestClient

from apps.agentic.llm_ab_router.server import create_app


class Store:
    def read(self):
        return {"disabled": [], "policy": {"request_timeout_seconds": 10}}, "etag"


def test_router_heartbeat_is_not_blocked_by_evidence_projection(monkeypatch):
    monkeypatch.setenv("AB_INTERNAL_TOKEN", "private-token")
    monkeypatch.setenv("AB_SCOPE", "workflow")
    monkeypatch.setenv("HOSTNAME", "recsys-workflow-router-test")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    beats = []

    class BrokenProjectionStore:
        def read(self):
            raise ValueError("new profile is not renderable by this projection")

    class Connection:
        def execute(self, statement, values=None):
            if "recsys_ab.heartbeat" in statement:
                beats.append(values)

    class BeatDatabase:
        def migrate(self):
            pass

        @contextmanager
        def connect(self):
            yield Connection()

    app = create_app("router", database=BeatDatabase(), store=BrokenProjectionStore(),
                     client=httpx.Client(transport=httpx.MockTransport(
                         lambda _: httpx.Response(500))))
    with TestClient(app):
        time.sleep(.05)
    assert beats == [("recsys-workflow-router-test",)]


def test_adapter_polling_preserves_substrate_context_and_never_replays(monkeypatch):
    monkeypatch.setenv("AB_INTERNAL_TOKEN", "private-token")
    monkeypatch.setenv("RELEASE_ID", "A")
    monkeypatch.setenv("A2A_UPSTREAM", "http://controller/a2a/A/")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr("apps.agentic.llm_ab_router.server.time.sleep", lambda _: None)
    calls = []

    def upstream(request):
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        calls.append(body)
        state = "TASK_STATE_WORKING" if len(calls) == 1 else "TASK_STATE_COMPLETED"
        return httpx.Response(
            200, json={"result": {"task": {"id": "task-1", "status": {"state": state}}}}
        )

    app = create_app(
        "adapter",
        store=Store(),
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/",
            headers={
                "authorization": "Bearer private-token",
                "a2a-version": "1.0",
            },
            json={
                "jsonrpc": "2.0",
                "id": "message-1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "contextId": "backend-context",
                        "messageId": "message-1",
                        "parts": [],
                    }
                },
            },
        )
    assert response.status_code == 200
    assert [c["method"] for c in calls] == ["SendMessage", "GetTask"]
    assert calls[1]["params"] == {"id": "task-1", "contextId": "backend-context"}


def test_recommendation_adapter_closes_stream_after_first_complete_tool_result(monkeypatch):
    monkeypatch.setenv("AB_INTERNAL_TOKEN", "private-token")
    monkeypatch.setenv("RELEASE_ID", "A")
    monkeypatch.setenv("A2A_UPSTREAM", "http://controller/a2a/A/")
    monkeypatch.setenv("RECOMMENDATION_OUTPUT_PROFILE", "trusted-tool-result-v1")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    calls = []
    tool_result = {
        "user_id": 1001,
        "items": [{"item_id": 7, "score": 0.9, "metadata": {"kind": "book"}}],
        "model_version": "ranker-v1",
        "ab_variant": "control",
        "ab_experiment_id": "exp",
    }

    def upstream(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["method"] == "message/stream"
        assert body["params"]["message"]["role"] == "user"
        assert request.headers["accept"] == "text/event-stream"
        assert "a2a-version" not in request.headers
        events = [
            # Exact legacy envelope emitted by the pinned native kagent
            # 0.10.0-rc1 Go SandboxAgent through the controller passthrough.
            {"jsonrpc": "2.0", "id": "message-1", "result": {
                "kind": "status-update", "taskId": "task-1",
                "contextId": "backend-context", "final": False,
                "status": {"state": "submitted", "message": {
                    "kind": "message", "messageId": "message-1",
                    "contextId": "backend-context", "role": "user",
                    "parts": [{"kind": "text", "text": "go"}],
                }},
            }},
            {"jsonrpc": "2.0", "id": "message-1", "result": {
                "kind": "status-update", "taskId": "task-1",
                "contextId": "backend-context", "final": False,
                "status": {"state": "working", "message": {
                    "kind": "message", "messageId": "call-message",
                    "contextId": "backend-context", "role": "agent",
                    "parts": [{
                        "kind": "data",
                        "data": {"id": "call-1", "name": "get_personalized_recommendations",
                                 "args": {"user_id": 1001, "candidate_item_ids": None, "top_k": 1}},
                        "metadata": {"adk_type": "function_call"},
                    }],
                }},
            }},
            {"jsonrpc": "2.0", "id": "message-1", "result": {
                "kind": "status-update", "taskId": "task-1",
                "contextId": "backend-context", "final": False,
                "status": {"state": "working", "message": {
                    "kind": "message", "messageId": "response-message",
                    "contextId": "backend-context", "role": "agent",
                    "parts": [{
                        "kind": "data",
                        "data": {"id": "call-1", "name": "get_personalized_recommendations",
                                 "response": {"output": tool_result}},
                        "metadata": {"adk_type": "function_response"},
                    }],
                }},
            }},
            # This duplicate would be unsafe; returning earlier proves the
            # adapter stops consuming immediately after the first pair.
            {"jsonrpc": "2.0", "id": "message-1", "result": {
                "kind": "status-update", "taskId": "task-1",
                "contextId": "backend-context", "final": False,
                "status": {"state": "working", "message": {
                    "kind": "message", "messageId": "duplicate-message",
                    "contextId": "backend-context", "role": "agent",
                    "parts": [{
                        "kind": "data",
                        "data": {"id": "call-2", "name": "get_personalized_recommendations",
                                 "args": {"user_id": 1001}},
                        "metadata": {"adk_type": "function_call"},
                    }],
                }},
            }},
        ]
        content = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    app = create_app(
        "adapter",
        store=Store(),
        client=httpx.Client(transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as client:
        response = client.post(
            "/",
            headers={"authorization": "Bearer private-token"},
            json={
                "jsonrpc": "2.0",
                "id": "message-1",
                "method": "SendMessage",
                "params": {
                    "message": {
                        "contextId": "backend-context",
                        "messageId": "message-1",
                        "role": "ROLE_USER",
                        "parts": [],
                    }
                },
            },
        )
    assert response.status_code == 200
    assert [call["method"] for call in calls] == ["message/stream"]
    task = response.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["metadata"]["native_stream_closed_after_tool_result"] is True
    assert json.loads(task["artifacts"][-1]["parts"][0]["text"]) == tool_result


def test_facade_strips_client_assignment_and_source(monkeypatch):
    monkeypatch.setenv("AB_INTERNAL_TOKEN", "private-token")
    monkeypatch.setenv("AB_ROUTER_URL", "http://router/")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    seen = []

    def router(request):
        seen.append(dict(request.headers))
        return httpx.Response(200, json={"result": {}})

    with TestClient(
        create_app("facade", client=httpx.Client(transport=httpx.MockTransport(router)))
    ) as client:
        response = client.post(
            "/",
            json={"method": "SendMessage"},
            headers={
                "x-recsys-release": "attacker-release",
                "x-recsys-source": "synthetic",
                "x-user-id": "user",
            },
        )
    assert response.status_code == 200
    assert "x-recsys-release" not in seen[0] and "x-recsys-source" not in seen[0]
    assert seen[0]["authorization"] == "Bearer private-token"
    assert seen[0]["x-user-id"] == "user"
