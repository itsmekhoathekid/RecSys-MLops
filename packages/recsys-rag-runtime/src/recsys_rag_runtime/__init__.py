"""Shared offline/online runtime for the RAG embedding contract."""

from recsys_rag_runtime.embedding import (
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    OnnxE5Encoder,
    TextEncoder,
    encode_with_fallback,
    sha256_file,
)

__all__ = [
    "PASSAGE_PREFIX",
    "QUERY_PREFIX",
    "OnnxE5Encoder",
    "TextEncoder",
    "encode_with_fallback",
    "sha256_file",
]
