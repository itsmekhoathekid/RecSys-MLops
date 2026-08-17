"""FastAPI composition root for grouped RAG item retrieval.

Startup loads only image-local model assets and existing Feast registry state.
The app never downloads model files or mutates an index. Readiness requires a
compatible active pointer; serving errors preserve the last-known-good pointer.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import boto3
from fastapi import FastAPI, HTTPException, Request

from recsys_rag_runtime import OnnxE5Encoder
from recsys_serving_common.runtime import configure_api, healthz, metrics, version_payload

from recsys_rag_api.contracts import RetrievalRequest, RetrievalResponse
from recsys_rag_api.pointer import ActivePointerManager, EmbeddingContract
from recsys_rag_api.retrieval import FeastCandidateSearch, RetrievalService
from recsys_rag_api.settings import RagApiSettings


def _pointer_loader(settings: RagApiSettings) -> Callable[[], bytes]:
    """Create a MinIO loader closure for the singleton active pointer."""

    client = boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        region_name="us-east-1",
    )

    def load() -> bytes:
        response = client.get_object(
            Bucket=settings.lake_bucket, Key=settings.active_pointer_key
        )
        return response["Body"].read()

    return load


def create_app(
    settings: RagApiSettings | None = None,
    retrieval_service: RetrievalService | None = None,
) -> FastAPI:
    """Build an injectable API; production dependencies initialize at startup."""

    settings = settings or RagApiSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if retrieval_service is None:
            from feast import FeatureStore

            contract = EmbeddingContract(
                model=settings.embedding_model,
                revision=settings.embedding_revision,
                dimension=settings.embedding_dimension,
            )
            pointers = ActivePointerManager(
                loader=_pointer_loader(settings),
                supported_contracts=[contract],
                reload_seconds=settings.pointer_reload_seconds,
            )
            service = RetrievalService(
                encoder=OnnxE5Encoder(
                    settings.model_dir, dimension=settings.embedding_dimension
                ),
                search=FeastCandidateSearch(
                    FeatureStore(repo_path=settings.feast_repo_path)
                ),
                pointers=pointers,
            )
        else:
            service = retrieval_service
        app.state.retrieval_service = service
        yield

    app = configure_api(
        FastAPI(title="RecSys RAG Item Retrieval API", version="0.1.0", lifespan=lifespan)
    )

    @app.get("/healthz")
    async def rag_healthz() -> dict[str, str]:
        """Report process liveness independent from index readiness."""

        return await healthz()

    @app.get("/ready")
    async def rag_ready(request: Request) -> dict[str, str]:
        """Require a model-compatible active pointer before accepting traffic."""

        try:
            await asyncio.to_thread(
                request.app.state.retrieval_service.pointers.get
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="active RAG index unavailable") from exc
        return {"status": "ready"}

    @app.get("/metrics")
    async def rag_metrics():
        """Expose the repository-standard Prometheus endpoint."""

        return await metrics()

    @app.get("/version")
    async def version() -> dict[str, object]:
        """Advertise model contracts so promotion can gate migrations safely."""

        payload = version_payload(
            "recsys-rag-api", feature_store="Feast", vector_store="Milvus"
        )
        payload["supported_embedding_contracts"] = [
            {
                "model": settings.embedding_model,
                "revision": settings.embedding_revision,
                "dimension": settings.embedding_dimension,
            }
        ]
        return payload

    @app.post("/v1/rag/retrieve", response_model=RetrievalResponse)
    async def retrieve(
        payload: RetrievalRequest, request: Request
    ) -> RetrievalResponse:
        """Encode a Vietnamese/multilingual query and return unique ranked items."""

        try:
            return await asyncio.to_thread(
                request.app.state.retrieval_service.retrieve, payload
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail="RAG retrieval failed") from exc

    return app


app = create_app()
