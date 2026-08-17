from __future__ import annotations

import numpy as np
import pytest

from recsys_rag_runtime.embedding import OnnxE5Encoder, encode_with_fallback


class Input:
    def __init__(self, name: str):
        self.name = name


class Tokenizer:
    def encode(self, text, add_special_tokens=False):
        return text.split()

    def __call__(self, texts, **kwargs):
        return {
            "input_ids": np.asarray([[1, 2], [3, 0]], dtype=np.int64),
            "attention_mask": np.asarray([[1, 1], [1, 0]], dtype=np.int64),
        }


class Session:
    def __init__(self):
        self.last_inputs = None

    def get_inputs(self):
        return [Input("input_ids"), Input("attention_mask"), Input("token_type_ids")]

    def run(self, outputs, inputs):
        self.last_inputs = inputs
        hidden = np.zeros((2, 2, 384), dtype=np.float32)
        hidden[:, :, 0] = 1.0
        return [hidden]


def test_encoder_supplies_segment_ids_and_normalizes_vectors():
    encoder = OnnxE5Encoder.__new__(OnnxE5Encoder)
    encoder.tokenizer = Tokenizer()
    encoder.session = Session()
    encoder.dimension = 384
    encoder.max_tokens = 384
    vectors = encoder.encode(["passage: one", "query: two"])
    assert encoder.session.last_inputs["token_type_ids"].tolist() == [[0, 0], [0, 0]]
    assert len(vectors) == 2
    assert all(np.linalg.norm(vector) == pytest.approx(1.0) for vector in vectors)


class MemoryLimitedEncoder:
    def encode(self, texts):
        if len(texts) > 8:
            raise RuntimeError("simulated OOM")
        return [[1.0] for _ in texts]


def test_batch_fallback_retries_at_smaller_size():
    assert len(encode_with_fallback(MemoryLimitedEncoder(), ["x"] * 17)) == 17
