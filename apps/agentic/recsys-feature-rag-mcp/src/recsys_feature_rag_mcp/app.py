"""FastAPI composition root mounting the stateless Streamable HTTP MCP app."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from recsys_feature_rag_mcp import __version__
from recsys_feature_rag_mcp.clients.online_features import OnlineFeatureClient
from recsys_feature_rag_mcp.clients.rag import RagClient
from recsys_feature_rag_mcp.server import create_mcp_server
from recsys_feature_rag_mcp.settings import McpSettings


def create_app(
    settings: McpSettings | None = None,
    feature_client: OnlineFeatureClient | None = None,
    rag_client: RagClient | None = None,
) -> FastAPI:
    """Compose FastAPI health endpoints with the authenticated MCP transport."""

    settings = settings or McpSettings.from_env()
    feature_client = feature_client or OnlineFeatureClient(
        base_url=settings.online_feature_api_url,
        timeout_seconds=settings.online_feature_timeout_seconds,
    )
    rag_client = rag_client or RagClient(
        base_url=settings.rag_api_url,
        timeout_seconds=settings.rag_timeout_seconds,
    )
    mcp = create_mcp_server(feature_client, rag_client)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Start MCP sessions and close pooled downstream HTTP clients."""

        async with mcp.session_manager.run():
            yield
        await feature_client.aclose()
        await rag_client.aclose()

    app = FastAPI(
        title="RecSys Feature and RAG MCP",
        version=__version__,
        lifespan=lifespan,
    )
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="healthz,ready,metrics",
    )

    @app.middleware("http")
    async def protect_mcp(request: Request, call_next):
        """Require a bearer token and accepted browser origin on MCP routes."""

        if request.url.path.startswith("/mcp"):
            if not settings.auth_token:
                return JSONResponse({"detail": "MCP authentication unavailable"}, 503)
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {settings.auth_token}"
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "unauthorized"}, 401)
            origin = request.headers.get("origin")
            if origin and origin not in settings.allowed_origins:
                return JSONResponse({"detail": "origin not allowed"}, 403)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Report process liveness without touching downstream services."""

        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Report readiness only when MCP authentication is configured."""

        if not settings.auth_token:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/version")
    async def version() -> dict[str, object]:
        """Expose immutable build and transport metadata."""

        return {
            "service": "recsys-feature-rag-mcp",
            "version": __version__,
            "image_reference": settings.image_reference,
            "transport": "streamable-http",
            "stateless": True,
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        """Expose Prometheus metrics for tool and downstream operations."""

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.mount("/", mcp.streamable_http_app())
    return app


app = create_app()
