"""FastAPI composition root for the recommendation MCP transport."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from recsys_recommendation_mcp import __version__
from recsys_recommendation_mcp.client import InferenceApiClient
from recsys_recommendation_mcp.server import create_mcp_server
from recsys_recommendation_mcp.settings import RecommendationMcpSettings


def create_app(
    settings: RecommendationMcpSettings | None = None,
    inference_client: InferenceApiClient | None = None,
) -> FastAPI:
    """Compose authenticated MCP with Kubernetes health and metrics endpoints."""

    settings = settings or RecommendationMcpSettings.from_env()
    inference_client = inference_client or InferenceApiClient(
        base_url=settings.inference_api_url,
        request_timeout_seconds=settings.request_timeout_seconds,
        total_deadline_seconds=settings.total_deadline_seconds,
        max_connections=settings.downstream_max_connections,
        max_keepalive_connections=settings.downstream_max_keepalive_connections,
    )
    mcp = create_mcp_server(inference_client, allowed_hosts=settings.allowed_hosts)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """Run MCP sessions and close the shared downstream transport."""

        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await inference_client.aclose()

    app = FastAPI(
        title="RecSys Recommendation MCP", version=__version__, lifespan=lifespan
    )
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="healthz,ready,metrics"
    )

    @app.middleware("http")
    async def protect_mcp(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Require the configured bearer token and accepted browser origins."""

        if request.url.path.startswith("/mcp"):
            if not settings.auth_token:
                return JSONResponse({"detail": "MCP authentication unavailable"}, 503)
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {settings.auth_token}"):
                return JSONResponse({"detail": "unauthorized"}, 401)
            origin = request.headers.get("origin")
            if origin and origin not in settings.allowed_origins:
                return JSONResponse({"detail": "origin not allowed"}, 403)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Report process liveness without a downstream call."""

        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Require MCP authentication before accepting traffic."""

        if not settings.auth_token:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/version")
    async def version() -> dict[str, object]:
        """Expose immutable build and transport metadata."""

        return {
            "service": "recsys-recommendation-mcp",
            "version": __version__,
            "image_reference": settings.image_reference,
            "transport": "streamable-http",
            "stateless": True,
            "downstream": "recsys-inference-api",
        }

    @app.get("/metrics")
    async def metrics() -> Response:
        """Expose Prometheus metrics."""

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    app.mount("/", mcp.streamable_http_app())
    return app


app = create_app()
