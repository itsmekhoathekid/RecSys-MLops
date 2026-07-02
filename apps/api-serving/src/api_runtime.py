from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Awaitable
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status

from observability import configure_logging, configure_tracing, log_event, metrics_text, observe_request


def configure_api(app: FastAPI) -> FastAPI:
    configure_logging()
    configure_tracing(app)

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        start = time.perf_counter()
        route = request.scope.get("path", request.url.path)
        method = request.method
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            log_event(
                "api request failed",
                logging.ERROR,
                component=os.getenv("RECSYS_API_COMPONENT", "api"),
                route=route,
                method=method,
                status=status_code,
                error_type=exc.__class__.__name__,
            )
            raise
        finally:
            duration = time.perf_counter() - start
            observe_request(route, method, status_code, duration)
            log_event(
                "api request completed",
                component=os.getenv("RECSYS_API_COMPONENT", "api"),
                route=route,
                method=method,
                status=status_code,
                duration_ms=round(duration * 1000, 3),
                model_version=os.getenv("MODEL_VERSION", "latest"),
            )

    return app


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def ready() -> dict[str, str]:
    if os.getenv("FORCE_NOT_READY") == "1":
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="forced not ready")
    return {"status": "ready"}


async def metrics() -> Response:
    return Response(metrics_text(), media_type="text/plain; version=0.0.4; charset=utf-8")


def version_payload(service: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "service": service,
        "model_version": os.getenv("MODEL_VERSION", "latest"),
    }
    payload.update(extra)
    return payload
