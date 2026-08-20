"""FastAPI composition root for grouped RAG item retrieval.

Startup loads only image-local model assets and existing Feast registry state.
The app never downloads model files or mutates an index. Readiness requires a
compatible active pointer; serving errors preserve the last-known-good pointer.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import boto3
from botocore.config import Config
from fastapi import FastAPI, HTTPException, Path, Request

from recsys_rag_runtime import OnnxE5Encoder
from recsys_serving_common.concurrency import BoundedExecutor, CapacityExceeded
from recsys_serving_common.runtime import configure_api, healthz, metrics, version_payload

from recsys_rag_api.batching import BatchingTextEncoder
from recsys_rag_api.chunk_lookup import (
    ChunkLookupService,
    configure_feast_milvus_bool_compatibility,
)
from recsys_rag_api.contracts import (
    ChunkBatchRequest,
    ChunkBatchResponse,
    ChunkResponse,
    RetrievalRequest,
    RetrievalResponse,
)
from recsys_rag_api.pointer import ActivePointerManager, EmbeddingContract
from recsys_rag_api.retrieval import MilvusCandidateSearch, RetrievalService
from recsys_rag_api.settings import RagApiSettings


def _configure_feast_registry_url() -> str:
    """Build Feast's escaped SQL registry URL from injected PostgreSQL env.

    Kubernetes injects the password separately from non-secret connection
    metadata. Keeping URL construction in-process avoids writing credentials to
    a ConfigMap or command line while still satisfying Feast's YAML expansion.
    """

    configured = os.getenv("FEAST_SQL_REGISTRY_URL", "").strip()
    if configured:
        return configured

    from sqlalchemy.engine import URL

    registry_url = URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("FEAST_POSTGRES_USER", "feast"),
        password=os.getenv("FEAST_POSTGRES_PASSWORD", "feast"),
        host=os.getenv(
            "FEAST_POSTGRES_HOST",
            "feature-postgres.recsys-dataflow.svc.cluster.local",
        ),
        port=int(os.getenv("FEAST_POSTGRES_PORT", "5432")),
        database=os.getenv("FEAST_POSTGRES_DB", "feature_store"),
        query={
            "sslmode": os.getenv("FEAST_POSTGRES_SSLMODE", "disable"),
            "options": (
                "-csearch_path="
                + os.getenv("FEAST_POSTGRES_SCHEMA", "feature_store")
            ),
        },
    ).render_as_string(hide_password=False)
    os.environ["FEAST_SQL_REGISTRY_URL"] = registry_url
    return registry_url


def _pointer_loader(settings: RagApiSettings) -> Callable[[], bytes]:
    """Create a MinIO loader closure for the singleton active pointer."""

    client = boto3.client(
        "s3",
        endpoint_url=settings.minio_endpoint,
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        region_name="us-east-1",
        config=Config(
            connect_timeout=settings.storage_timeout_seconds,
            read_timeout=settings.storage_timeout_seconds,
            retries={"max_attempts": 1},
        ),
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
    chunk_lookup_service: ChunkLookupService | None = None,
) -> FastAPI:
    """Build an injectable API; production dependencies initialize at startup."""

    settings = settings or RagApiSettings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        request_executor = BoundedExecutor(
            workers=settings.sync_workers,
            queue_size=settings.sync_queue_size,
            wait_seconds=settings.capacity_wait_seconds,
            operation="rag_sync",
        )
        control_executor = BoundedExecutor(
            workers=1,
            queue_size=1,
            wait_seconds=settings.capacity_wait_seconds,
            operation="rag_control",
        )
        batching_encoder: BatchingTextEncoder | None = None
        milvus_client: object | None = None
        exact_lookup = chunk_lookup_service
        if retrieval_service is None:
            from feast import FeatureStore
            from pymilvus import MilvusClient

            _configure_feast_registry_url()
            configure_feast_milvus_bool_compatibility()
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
            batching_encoder = BatchingTextEncoder(
                OnnxE5Encoder(
                    settings.model_dir, dimension=settings.embedding_dimension
                )
            )
            feature_store = FeatureStore(repo_path=settings.feast_repo_path)
            # Feast is the registry/schema authority. Resolve both slots at
            # startup so a malformed FeatureView fails before serving traffic.
            for feature_view in ("rag_item_chunks_blue", "rag_item_chunks_green"):
                feature_store.get_feature_view(feature_view)
            milvus_client = MilvusClient(
                uri=f"{settings.milvus_host}:{settings.milvus_port}",
                token=(
                    f"{settings.milvus_username}:{settings.milvus_password}"
                    if settings.milvus_username and settings.milvus_password
                    else ""
                ),
            )
            service = RetrievalService(
                encoder=batching_encoder,
                search=MilvusCandidateSearch(
                    milvus_client,
                    project=feature_store.project,
                    timeout_seconds=settings.storage_timeout_seconds,
                ),
                pointers=pointers,
            )
            exact_lookup = exact_lookup or ChunkLookupService(
                feature_store=feature_store,
                pointers=pointers,
            )
        else:
            service = retrieval_service
        app.state.retrieval_service = service
        app.state.chunk_lookup_service = exact_lookup
        app.state.request_executor = request_executor
        app.state.control_executor = control_executor
        try:
            yield
        finally:
            await request_executor.aclose()
            await control_executor.aclose()
            if batching_encoder is not None:
                batching_encoder.close()
            if milvus_client is not None:
                milvus_client.close()

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
            await request.app.state.control_executor.run(
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
            return await request.app.state.request_executor.run(
                request.app.state.retrieval_service.retrieve, payload
            )
        except CapacityExceeded as exc:
            raise HTTPException(status_code=503, detail="RAG capacity exhausted") from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="RAG retrieval failed") from exc

    @app.get("/v1/rag/chunks/{chunk_id}", response_model=ChunkResponse)
    async def get_chunk(
        request: Request,
        chunk_id: str = Path(min_length=1, max_length=512),
    ) -> ChunkResponse:
        """Return one chunk from the active online FeatureView by stable ID."""

        service = request.app.state.chunk_lookup_service
        if service is None:
            raise HTTPException(status_code=503, detail="chunk lookup unavailable")
        try:
            result = await request.app.state.request_executor.run(service.get_many, [chunk_id])
        except CapacityExceeded as exc:
            raise HTTPException(status_code=503, detail="RAG capacity exhausted") from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail="active RAG index unavailable"
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="chunk lookup failed") from exc
        if not result.chunks:
            raise HTTPException(status_code=404, detail="chunk not found")
        return ChunkResponse(
            **result.chunks[0].model_dump(),
            pipeline_run_id=result.pipeline_run_id,
        )

    @app.post("/v1/rag/chunks:batch-get", response_model=ChunkBatchResponse)
    async def batch_get_chunks(
        payload: ChunkBatchRequest, request: Request
    ) -> ChunkBatchResponse:
        """Return ordered chunks and explicitly report IDs not materialized."""

        service = request.app.state.chunk_lookup_service
        if service is None:
            raise HTTPException(status_code=503, detail="chunk lookup unavailable")
        try:
            return await request.app.state.request_executor.run(service.get_many, payload.chunk_ids)
        except CapacityExceeded as exc:
            raise HTTPException(status_code=503, detail="RAG capacity exhausted") from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=503, detail="active RAG index unavailable"
            ) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="chunk lookup failed") from exc

    return app


app = create_app()
