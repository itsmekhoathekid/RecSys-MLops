#!/usr/bin/env bash

recommendation_mcp_protocol_smoke() {
  kubectl -n kagent rollout status deployment/recsys-recommendation-mcp \
    --timeout="${timeout}"
  kubectl -n kagent exec deployment/recsys-recommendation-mcp -c mcp -- python -c '
import asyncio
import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"]}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8080/mcp", http_client=http_client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert [tool.name for tool in tools.tools] == [
                    "get_personalized_recommendations"
                ]
                result = await session.call_tool(
                    "get_personalized_recommendations",
                    {"user_id": int(os.getenv("RECOMMENDATION_SMOKE_USER_ID", "1001")), "top_k": 1},
                )
                assert not result.isError

asyncio.run(main())
'
}
agentic_mcp_protocol_smoke() {
  kubectl -n kagent rollout status deployment/recsys-feature-rag-mcp \
    --timeout="${timeout}"
  kubectl -n kagent exec deployment/recsys-feature-rag-mcp -c mcp -- python -c '
import asyncio
import os
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    headers = {"Authorization": "Bearer " + os.environ["MCP_AUTH_TOKEN"]}
    async with httpx.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(
            "http://127.0.0.1:8080/mcp", http_client=http_client
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                expected = {
                    "get_user_online_features",
                    "get_chunk_by_id",
                    "retrieve_rag_context",
                    "build_user_rag_context",
                }
                assert {tool.name for tool in tools.tools} == expected

asyncio.run(main())
'
}
