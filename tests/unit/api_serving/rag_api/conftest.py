from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from recsys_rag_api.app import create_app
from recsys_rag_api.contracts import (
    CandidateChunk,
    ChunkBatchResponse,
    ChunkRecord,
)
from recsys_rag_api.retrieval import RetrievalService
from recsys_rag_api.settings import RagApiSettings


def rag_settings() -> RagApiSettings:
    return RagApiSettings(
        feast_repo_path="/tmp/feast",
        model_dir="/tmp/model",
        lake_bucket="lake",
        active_pointer_key="gold/_active/pointer.json",
        minio_endpoint="http://minio",
        minio_access_key="access",
        minio_secret_key="secret",
        milvus_host="http://milvus",
        milvus_port=19530,
        milvus_username="root",
        milvus_password="password",
        embedding_model="intfloat/multilingual-e5-small",
        embedding_revision="revision-1",
        embedding_dimension=384,
        pointer_reload_seconds=60,
    )


class DeterministicEncoder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(len(texts[0])), 1.0]]

    def token_count(self, text: str) -> int:
        return len(text.split())


class DeterministicSearch:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[float], int]] = []

    def search(
        self, *, feature_view: str, query_vector: list[float], top_k: int
    ) -> list[CandidateChunk]:
        self.calls.append((feature_view, query_vector, top_k))
        return [
            CandidateChunk(
                chunk_id=f"{item_id}:overview",
                item_id=item_id,
                chunk_type="product_overview",
                source_key=f"items/{item_id}",
                text=f"item {item_id}",
                brand="Acme",
                category_l1="Audio",
                category_l2="Headphones",
                current_price=float(item_id),
                in_stock=item_id % 2 == 1,
                average_rating=5.0 - item_id / 100,
                score=1.0 - item_id / 100,
            )
            for item_id in range(1, 21)
        ][:top_k]


class DeterministicPointers:
    def __init__(self) -> None:
        self.calls = 0

    def get(self) -> SimpleNamespace:
        self.calls += 1
        return SimpleNamespace(
            feature_view="rag_item_chunks_blue",
            pipeline_run_id="rag-proof-run",
        )


class DeterministicChunkService:
    def get_many(self, chunk_ids: list[str]) -> ChunkBatchResponse:
        chunks = [
            ChunkRecord(
                chunk_id=chunk_id,
                item_id=index + 1,
                chunk_type="product_overview",
                source_key=f"items/{index + 1}",
                text=f"chunk {chunk_id}",
                brand="Acme",
                current_price=10.0,
                in_stock=True,
                average_rating=4.5,
                source_run_id="source-1",
            )
            for index, chunk_id in enumerate(chunk_ids)
            if chunk_id != "missing"
        ]
        return ChunkBatchResponse(
            pipeline_run_id="rag-proof-run",
            chunks=chunks,
            missing_chunk_ids=[
                chunk_id for chunk_id in chunk_ids if chunk_id == "missing"
            ],
        )


@pytest.fixture
def rag_api() -> TestClient:
    encoder = DeterministicEncoder()
    search = DeterministicSearch()
    pointers = DeterministicPointers()
    retrieval_implementation = RetrievalService(
        encoder=encoder,
        search=search,
        pointers=pointers,
    )
    retrieval = Mock(spec=RetrievalService, wraps=retrieval_implementation)
    retrieval.pointers = pointers
    chunk_implementation = DeterministicChunkService()
    chunks = Mock(spec=DeterministicChunkService, wraps=chunk_implementation)
    app = create_app(
        rag_settings(),
        retrieval_service=retrieval,
        chunk_lookup_service=chunks,
    )
    with TestClient(app) as client:
        client.app.state.retrieval_service_mock = retrieval
        client.app.state.chunk_service_mock = chunks
        client.app.state.encoder = encoder
        client.app.state.search = search
        client.app.state.pointers = pointers
        yield client
