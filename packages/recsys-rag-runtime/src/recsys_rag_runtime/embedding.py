"""Image-local ONNX E5 encoder shared by batch indexing and retrieval.

The model and tokenizer must already exist in the container. Runtime downloads
are disabled. Mean pooling and L2 normalization are centralized here so online
queries and offline passages cannot drift through independent implementations.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np


PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "


class TextEncoder(Protocol):
    """Small encoder interface used by production and deterministic test fakes."""

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Return normalized vectors in input order."""

    def token_count(self, text: str) -> int:
        """Count text with the exact packaged model tokenizer."""


def sha256_file(path: str | Path) -> str:
    """Calculate a streaming SHA-256 checksum for a model artifact."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


class OnnxE5Encoder:
    """Run a local dynamic-INT8 E5 ONNX model on CPU."""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        dimension: int = 384,
        max_tokens: int = 384,
    ) -> None:
        model_path = Path(model_dir) / "model_quantized.onnx"
        if not model_path.is_file():
            raise FileNotFoundError(
                f"Packaged ONNX model is missing: {model_path}; runtime downloads are disabled"
            )
        import onnxruntime as ort
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(
            str(model_dir), local_files_only=True
        )
        self.session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
        self.dimension = dimension
        self.max_tokens = max_tokens

    def token_count(self, text: str) -> int:
        """Count tokens without model special tokens."""

        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Mean-pool, normalize, and validate one text batch."""

        if not texts:
            return []
        encoded = self.tokenizer(
            list(texts),
            padding=True,
            truncation=True,
            max_length=self.max_tokens,
            return_tensors="np",
        )
        inputs: dict[str, np.ndarray] = {}
        for item in self.session.get_inputs():
            if item.name in encoded:
                inputs[item.name] = encoded[item.name].astype(np.int64)
            elif item.name == "token_type_ids":
                # Some XLM-R tokenizers omit segment IDs even though the exported
                # ONNX graph keeps that BERT-compatible input. E5 uses one segment,
                # so an all-zero tensor is the exact intended representation.
                inputs[item.name] = np.zeros_like(
                    encoded["input_ids"], dtype=np.int64
                )
            else:
                raise RuntimeError(f"Tokenizer did not produce ONNX input {item.name!r}")
        hidden = self.session.run(None, inputs)[0]
        attention = encoded["attention_mask"].astype(np.float32)[..., None]
        pooled = (hidden * attention).sum(axis=1) / np.maximum(
            attention.sum(axis=1), 1e-12
        )
        vectors = pooled / np.maximum(
            np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12
        )
        if vectors.shape != (len(texts), self.dimension):
            raise RuntimeError(
                f"Expected {(len(texts), self.dimension)} vectors, got {vectors.shape}"
            )
        if not np.isfinite(vectors).all():
            raise RuntimeError("Encoder returned NaN or infinity")
        return vectors.astype(np.float32).tolist()


def encode_with_fallback(
    encoder: TextEncoder,
    texts: Sequence[str],
    *,
    batch_sizes: Sequence[int] = (32, 16, 8),
) -> list[list[float]]:
    """Retry memory-constrained batches at 32, 16, then 8 records."""

    last_error: Exception | None = None
    for batch_size in batch_sizes:
        try:
            output: list[list[float]] = []
            for start in range(0, len(texts), batch_size):
                output.extend(encoder.encode(texts[start : start + batch_size]))
            return output
        except (MemoryError, RuntimeError) as exc:
            last_error = exc
    raise RuntimeError("Embedding failed at batch sizes 32, 16, and 8") from last_error
