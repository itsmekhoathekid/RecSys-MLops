"""Deterministic RAG API used to produce reproducible local Locust evidence."""

from __future__ import annotations

from types import SimpleNamespace

from recsys_rag_api.app import create_app
from recsys_rag_api.contracts import CandidateChunk
from recsys_rag_api.retrieval import RetrievalService
from recsys_rag_api.settings import RagApiSettings


class DeterministicEncoder:
    """Avoid model I/O while preserving the production retrieval code path."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class DeterministicSearch:
    """Return stable in-stock chunks for a load test without external stores."""

    def search(
        self, *, feature_view: str, query_vector: list[float], top_k: int
    ) -> list[CandidateChunk]:
        del feature_view, query_vector
        limit = min(top_k, 20)
        return [
            CandidateChunk(
                chunk_id=f"chunk-{item_id}",
                item_id=item_id,
                chunk_type="product_overview",
                source_key=f"items/{item_id}",
                text=f"Deterministic product evidence {item_id}",
                brand="Acme",
                category_l1="Electronics",
                category_l2="Audio",
                current_price=100.0 + item_id,
                in_stock=True,
                average_rating=4.5,
                score=1.0 - (item_id / 1000),
            )
            for item_id in range(1, limit + 1)
        ]


SETTINGS = RagApiSettings(
    feast_repo_path="/tmp/unused-feast",
    model_dir="/tmp/unused-model",
    lake_bucket="unused",
    active_pointer_key="unused",
    minio_endpoint="http://unused",
    minio_access_key="unused",
    minio_secret_key="unused",
    milvus_host="http://unused",
    milvus_port=19530,
    milvus_username="unused",
    milvus_password="unused",
    embedding_model="intfloat/multilingual-e5-small",
    embedding_revision="load-proof",
    embedding_dimension=2,
    pointer_reload_seconds=60,
)

SERVICE = RetrievalService(
    encoder=DeterministicEncoder(),
    search=DeterministicSearch(),
    pointers=SimpleNamespace(
        get=lambda: SimpleNamespace(
            feature_view="rag_item_chunks_blue",
            pipeline_run_id="rag-load-proof",
        )
    ),
)

app = create_app(SETTINGS, retrieval_service=SERVICE)
