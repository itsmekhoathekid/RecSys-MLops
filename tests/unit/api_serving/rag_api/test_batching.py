from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from recsys_rag_api.batching import BatchingTextEncoder


class _RecordingEncoder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts):
        values = list(texts)
        self.calls.append(values)
        return [[float(int(value))] for value in values]

    def token_count(self, text: str) -> int:
        return len(text)


def test_concurrent_queries_share_one_bounded_delegate_call() -> None:
    delegate = _RecordingEncoder()
    encoder = BatchingTextEncoder(
        delegate, max_batch_size=32, max_wait_seconds=0.05
    )
    barrier = threading.Barrier(10)

    def encode(value: int) -> list[list[float]]:
        barrier.wait()
        return encoder.encode([str(value)])

    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(encode, range(10)))
    finally:
        encoder.close()

    assert len(delegate.calls) == 1
    assert sorted(delegate.calls[0], key=int) == [str(value) for value in range(10)]
    assert sorted(result[0][0] for result in results) == list(map(float, range(10)))


def test_delegate_failure_reaches_every_caller() -> None:
    class FailingEncoder(_RecordingEncoder):
        def encode(self, texts):
            raise RuntimeError("encoder failed")

    encoder = BatchingTextEncoder(FailingEncoder(), max_wait_seconds=0)
    try:
        with pytest.raises(RuntimeError, match="encoder failed"):
            encoder.encode(["1"])
    finally:
        encoder.close()


def test_configuration_and_token_count_validation() -> None:
    delegate = _RecordingEncoder()
    with pytest.raises(ValueError, match="max_batch_size"):
        BatchingTextEncoder(delegate, max_batch_size=0)
    with pytest.raises(ValueError, match="max_wait_seconds"):
        BatchingTextEncoder(delegate, max_wait_seconds=-1)

    encoder = BatchingTextEncoder(delegate)
    try:
        assert encoder.token_count("abc") == 3
        assert encoder.encode([]) == []
    finally:
        encoder.close()
