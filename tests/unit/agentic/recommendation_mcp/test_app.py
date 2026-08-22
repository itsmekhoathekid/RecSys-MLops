from __future__ import annotations

from fastapi.testclient import TestClient
from recsys_recommendation_mcp.app import create_app
from recsys_recommendation_mcp.contracts import RecommendationResponse
from recsys_recommendation_mcp.settings import RecommendationMcpSettings


class InferenceClient:
    def __init__(self) -> None:
        self.calls = 0

    async def aclose(self) -> None:
        return None

    async def recommend(self, **kwargs: object) -> RecommendationResponse:
        self.calls += 1
        return RecommendationResponse.model_validate(
            {
                "user_id": kwargs["user_id"],
                "model_version": "bst-v7",
                "items": [{"item_id": 2, "score": 0.9}],
            }
        )


def settings(token: str = "secret") -> RecommendationMcpSettings:
    return RecommendationMcpSettings(
        inference_api_url="http://inference",
        auth_token=token,
        allowed_origins=("https://kagent.example",),
        allowed_hosts=(
            "127.0.0.1:*",
            "recsys-recommendation-mcp.kagent.svc.cluster.local:8080",
        ),
        image_reference="registry/recsys-recommendation-mcp@sha256:abc",
    )


def test_health_readiness_version_metrics_and_auth() -> None:
    dependency = InferenceClient()
    with TestClient(create_app(settings(), dependency)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/ready").json() == {"status": "ready"}
        assert client.get("/version").json()["downstream"] == "recsys-inference-api"
        assert client.get("/metrics").status_code == 200
        assert client.post("/mcp").status_code == 401


def test_protocol_lists_and_calls_only_recommendation_tool() -> None:
    dependency = InferenceClient()
    headers = {
        "Host": "127.0.0.1:8080",
        "Authorization": "Bearer secret",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(create_app(settings(), dependency)) as client:
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
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        headers["MCP-Protocol-Version"] = "2025-06-18"
        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ).json()
        assert [item["name"] for item in listed["result"]["tools"]] == [
            "get_personalized_recommendations"
        ]
        called = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_personalized_recommendations",
                    "arguments": {"user_id": 1001, "top_k": 1},
                },
            },
        ).json()
        assert called["result"]["isError"] is False
        invalid = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "get_personalized_recommendations",
                    "arguments": {"user_id": 0},
                },
            },
        ).json()
        assert invalid["result"]["isError"] is True
        assert dependency.calls == 1
