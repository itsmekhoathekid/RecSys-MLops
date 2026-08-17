from __future__ import annotations

import os

# Unit tests exercise HTTP contracts without starting an OTLP exporter thread.
os.environ.setdefault("RECSYS_OTEL_ENABLED", "0")

from fastapi.testclient import TestClient

from recsys_rag_api.app import _configure_feast_registry_url, create_app
from recsys_rag_api.contracts import RetrievalResponse
from recsys_rag_api.settings import RagApiSettings


class Pointers:
    def __init__(self, error: bool = False):
        self.error = error

    def get(self):
        if self.error:
            raise RuntimeError("pointer unavailable")
        return object()


class Service:
    def __init__(self, *, pointer_error: bool = False, retrieve_error: bool = False):
        self.pointers = Pointers(pointer_error)
        self.retrieve_error = retrieve_error

    def retrieve(self, request):
        if self.retrieve_error:
            raise RuntimeError("search unavailable")
        return RetrievalResponse(query=request.query, pipeline_run_id="run-1", items=[])


def settings() -> RagApiSettings:
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


def test_health_ready_version_metrics_and_retrieve_contract():
    with TestClient(create_app(settings(), Service())) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/ready").json() == {"status": "ready"}
        version = client.get("/version").json()
        assert version["supported_embedding_contracts"][0]["dimension"] == 384
        assert client.get("/metrics").status_code == 200
        response = client.post("/v1/rag/retrieve", json={"query": " tai nghe "})
        assert response.status_code == 200
        assert response.json()["query"] == "tai nghe"


def test_readiness_and_retrieval_failures_are_classified():
    with TestClient(create_app(settings(), Service(pointer_error=True))) as client:
        assert client.get("/ready").status_code == 503
    with TestClient(create_app(settings(), Service(retrieve_error=True))) as client:
        assert client.post("/v1/rag/retrieve", json={"query": "test"}).status_code == 502


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("RAG_EMBEDDING_DIMENSION", "384")
    monkeypatch.setenv("RAG_POINTER_RELOAD_SECONDS", "15")
    monkeypatch.setenv("RAG_EMBEDDING_REVISION", "env-revision")
    loaded = RagApiSettings.from_env()
    assert loaded.embedding_revision == "env-revision"
    assert loaded.embedding_dimension == 384
    assert loaded.pointer_reload_seconds == 15


def test_feast_registry_url_is_built_without_exposing_unescaped_password(monkeypatch):
    from sqlalchemy.engine import make_url

    monkeypatch.delenv("FEAST_SQL_REGISTRY_URL", raising=False)
    monkeypatch.setenv("FEAST_POSTGRES_USER", "rag_user")
    monkeypatch.setenv("FEAST_POSTGRES_PASSWORD", "p@ss:/word")
    monkeypatch.setenv("FEAST_POSTGRES_HOST", "feature-postgres.internal")
    monkeypatch.setenv("FEAST_POSTGRES_DB", "feature_store")
    monkeypatch.setenv("FEAST_POSTGRES_SCHEMA", "rag")

    configured = _configure_feast_registry_url()
    parsed = make_url(configured)

    assert parsed.username == "rag_user"
    assert parsed.password == "p@ss:/word"
    assert parsed.host == "feature-postgres.internal"
    assert parsed.query["options"] == "-csearch_path=rag"
