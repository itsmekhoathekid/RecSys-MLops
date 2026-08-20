#!/usr/bin/env bash
set -euo pipefail

image="${1:?image reference is required}"
service="${2:?service name is required}"

case "${service}" in
  recsys-online-feature-api)
    docker run --rm "${image}" python -c '
import importlib.util
from recsys_online_feature_api.app import app
assert app.title == "RecSys Online Feature API"
assert importlib.util.find_spec("tritonclient") is None
'
    ;;
  recsys-inference-api)
    docker run --rm "${image}" python -c '
import importlib.util
from recsys_inference_api.app import app
assert app.title == "RecSys Inference API"
for module in ("feast", "redis", "psycopg"):
    assert importlib.util.find_spec(module) is None, module
'
    ;;
  recsys-rag-api)
    docker run --rm "${image}" python -c '
import json
from pathlib import Path
from recsys_rag_api.app import app
assert app.title == "RecSys RAG Item Retrieval API"
manifest = json.loads(Path("/opt/recsys/models/multilingual-e5-small/model_manifest.json").read_text())
assert manifest["dimension"] == 384
assert manifest["sha256"].startswith("sha256:")
'
    ;;
  recsys-feature-rag-mcp)
    docker run --rm "${image}" python -c '
import asyncio
import importlib.util

from fastapi.testclient import TestClient
from recsys_feature_rag_mcp.app import app
from recsys_feature_rag_mcp.server import TOOL_NAMES, create_mcp_server

assert app.title == "RecSys Feature and RAG MCP"
for module in ("feast", "pymilvus", "redis"):
    assert importlib.util.find_spec(module) is None, module
with TestClient(app) as client:
    assert client.get("/healthz").status_code == 200
    assert client.get("/version").json()["transport"] == "streamable-http"

class UnusedClient:
    pass

async def verify_tools():
    tools = await create_mcp_server(UnusedClient(), UnusedClient()).list_tools()
    assert tuple(tool.name for tool in tools) == TOOL_NAMES

asyncio.run(verify_tools())
'
    ;;
  *)
    printf 'unsupported serving image: %s\n' "${service}" >&2
    exit 2
    ;;
esac
