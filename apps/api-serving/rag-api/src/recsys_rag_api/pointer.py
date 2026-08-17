"""Periodic active-index pointer reload with last-known-good fallback.

Pointer objects are validated against the API's supported embedding contracts.
An absent or incompatible new object never replaces a previously valid pointer,
which keeps serving available during a bad promotion while readiness exposes a
cold-start failure when no valid pointer has ever loaded.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field


class ActivePointer(BaseModel):
    """Subset of the published pointer required by retrieval."""

    model_config = ConfigDict(extra="ignore")
    active_slot: str
    feature_view: str
    pipeline_run_id: str
    source_run_id: str
    chunker_version: str
    embedding_model: str
    embedding_revision: str
    embedding_dimension: int = Field(gt=0)


@dataclass(frozen=True)
class EmbeddingContract:
    """Embedding model revision and dimension supported by this API image."""

    model: str
    revision: str
    dimension: int

    def supports(self, pointer: ActivePointer) -> bool:
        """Return true when query and passage vectors share one contract."""

        return (
            self.model == pointer.embedding_model
            and self.revision == pointer.embedding_revision
            and self.dimension == pointer.embedding_dimension
        )


class ActivePointerManager:
    """Thread-safe, TTL-based pointer cache that preserves last-known-good state."""

    def __init__(
        self,
        *,
        loader: Callable[[], bytes],
        supported_contracts: list[EmbeddingContract],
        reload_seconds: float = 60.0,
    ) -> None:
        self.loader = loader
        self.supported_contracts = supported_contracts
        self.reload_seconds = reload_seconds
        self._pointer: ActivePointer | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> ActivePointer:
        """Return a validated pointer, reloading after the configured TTL.

        Raises:
            RuntimeError: No valid active pointer has ever loaded.
        """

        now = time.monotonic()
        with self._lock:
            if self._pointer is not None and now - self._loaded_at < self.reload_seconds:
                return self._pointer
            try:
                candidate = ActivePointer.model_validate(
                    json.loads(self.loader().decode("utf-8"))
                )
                if not any(
                    contract.supports(candidate) for contract in self.supported_contracts
                ):
                    raise ValueError("Active pointer embedding contract is unsupported")
                self._pointer = candidate
                self._loaded_at = now
            except Exception:
                # A malformed promotion must not evict the last-known-good pointer.
                # Cold start still fails readiness because no compatible query
                # encoder exists for an unvalidated collection.
                if self._pointer is None:
                    raise RuntimeError("No valid active RAG index pointer")
            return self._pointer
