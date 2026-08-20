from __future__ import annotations

from fastapi.testclient import TestClient
from recsys_feature_rag_mcp.app import create_app
from recsys_feature_rag_mcp.settings import McpSettings


class Client:
    async def aclose(self):
        pass

    async def get_features(self, **kwargs):
        return {"user_id": kwargs["user_id"]}

    async def get_chunk(self, chunk_id):
        return {"chunk_id": chunk_id}

    async def retrieve(self, **kwargs):
        return {"query": kwargs["query"]}


def settings(token: str = "secret") -> McpSettings:
    return McpSettings(
        online_feature_api_url="http://feature",
        rag_api_url="http://rag",
        auth_token=token,
        allowed_origins=("https://kagent.example",),
        image_reference="registry/recsys-feature-rag-mcp@sha256:abc",
    )


def test_health_ready_version_metrics_and_mcp_authentication():
    client_dependency = Client()
    with TestClient(
        create_app(settings(), client_dependency, client_dependency)
    ) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}
        assert client.get("/version").json()["stateless"] is True
        assert client.get("/metrics").status_code == 200
        assert client.post("/mcp").status_code == 401
        assert (
            client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer secret",
                    "Origin": "https://evil.example",
                },
            ).status_code
            == 403
        )


def test_missing_server_token_fails_readiness_and_mcp_closed():
    client_dependency = Client()
    with TestClient(
        create_app(settings(""), client_dependency, client_dependency)
    ) as client:
        assert client.get("/ready").status_code == 503
        assert client.post("/mcp").status_code == 503


def test_streamable_http_initialize_list_and_all_tool_calls():
    dependency = Client()
    headers = {
        "Host": "127.0.0.1:8080",
        "Authorization": "Bearer secret",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(create_app(settings(), dependency, dependency)) as client:
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "contract-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["protocolVersion"] == "2025-06-18"
        headers["MCP-Protocol-Version"] = "2025-06-18"

        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ).json()
        assert [tool["name"] for tool in listed["result"]["tools"]] == [
            "get_user_online_features",
            "get_chunk_by_id",
            "retrieve_rag_context",
            "build_user_rag_context",
        ]

        calls = {
            "get_user_online_features": {"user_id": 7},
            "get_chunk_by_id": {"chunk_id": "chunk-1"},
            "retrieve_rag_context": {"query": "headphones"},
            "build_user_rag_context": {"user_id": 7, "query": "headphones"},
        }
        for request_id, (name, arguments) in enumerate(calls.items(), start=3):
            body = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            ).json()
            assert body["result"]["isError"] is False

        invalid = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "get_user_online_features",
                    "arguments": {"user_id": "not-an-integer"},
                },
            },
        ).json()
        assert invalid["result"]["isError"] is True
        assert "validation" in invalid["result"]["content"][0]["text"].lower()
