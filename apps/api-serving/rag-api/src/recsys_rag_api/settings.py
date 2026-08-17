"""Environment-backed immutable settings for the RAG retrieval API."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RagApiSettings:
    """Runtime paths, storage endpoints, and the supported embedding contract."""

    feast_repo_path: str
    model_dir: str
    lake_bucket: str
    active_pointer_key: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    embedding_model: str
    embedding_revision: str
    embedding_dimension: int
    pointer_reload_seconds: float

    @classmethod
    def from_env(cls) -> "RagApiSettings":
        """Load settings without ever printing secret-backed values."""

        return cls(
            feast_repo_path=os.getenv(
                "RAG_FEAST_REPO",
                "/opt/recsys/apps/data-platform/feature-store/rag_feature_repo",
            ),
            model_dir=os.getenv(
                "RAG_MODEL_DIR", "/opt/recsys/models/multilingual-e5-small"
            ),
            lake_bucket=os.getenv("LAKE_BUCKET", "recsys-lakehouse"),
            active_pointer_key=os.getenv(
                "RAG_ACTIVE_POINTER_KEY",
                "gold/rag_item_embeddings/_active/pointer.json",
            ),
            minio_endpoint=os.getenv(
                "MINIO_ENDPOINT", "http://data-platform-minio:9000"
            ),
            minio_access_key=os.getenv("AWS_ACCESS_KEY_ID", ""),
            minio_secret_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
            embedding_model=os.getenv(
                "RAG_EMBEDDING_MODEL", "intfloat/multilingual-e5-small"
            ),
            embedding_revision=os.getenv(
                "RAG_EMBEDDING_REVISION",
                "03415a4be176a1620747c692ed433219fabc3def",
            ),
            embedding_dimension=int(os.getenv("RAG_EMBEDDING_DIMENSION", "384")),
            pointer_reload_seconds=float(
                os.getenv("RAG_POINTER_RELOAD_SECONDS", "60")
            ),
        )
