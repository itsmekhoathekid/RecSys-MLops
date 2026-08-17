"""Thread-safe micro-batching for the synchronous ONNX query encoder.

FastAPI dispatches retrieval work to a thread pool. Without this adapter, a
concurrency burst invokes the same ONNX session once per query and contends for
the pod's two CPU cores. The adapter gathers requests for a few milliseconds,
encodes at most 32 texts together, and returns each caller only its own vectors.
Failures are broadcast to every caller in the affected batch.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Sequence

from recsys_rag_runtime import TextEncoder


@dataclass
class _EncodeRequest:
    texts: tuple[str, ...]
    completed: threading.Event = field(default_factory=threading.Event)
    result: list[list[float]] | None = None
    error: BaseException | None = None


class BatchingTextEncoder:
    """Combine concurrent synchronous encoder calls into bounded ONNX batches.

    ``encode`` remains synchronous so it can replace any ``TextEncoder`` in the
    retrieval service. ``close`` stops the daemon worker during FastAPI shutdown.
    The delegate is called by one worker only, avoiding concurrent access to the
    same ONNX session while retaining request-level concurrency after encoding.
    """

    _STOP = object()

    def __init__(
        self,
        delegate: TextEncoder,
        *,
        max_batch_size: int = 32,
        max_wait_seconds: float = 0.01,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be at least 1")
        if max_wait_seconds < 0:
            raise ValueError("max_wait_seconds cannot be negative")
        self.delegate = delegate
        self.max_batch_size = max_batch_size
        self.max_wait_seconds = max_wait_seconds
        self._requests: queue.Queue[_EncodeRequest | object] = queue.Queue()
        self._worker = threading.Thread(
            target=self._run,
            name="rag-query-encoder-batcher",
            daemon=True,
        )
        self._worker.start()

    def token_count(self, text: str) -> int:
        """Delegate token counting without queueing a model inference."""

        return self.delegate.token_count(text)

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        """Queue texts and block until their shared ONNX batch completes."""

        if not texts:
            return []
        if len(texts) > self.max_batch_size:
            # Offline callers already batch independently. Keeping this adapter
            # bounded prevents one unexpected API call from starving the queue.
            return self.delegate.encode(texts)
        request = _EncodeRequest(tuple(texts))
        self._requests.put(request)
        request.completed.wait()
        if request.error is not None:
            raise request.error
        assert request.result is not None
        return request.result

    def close(self) -> None:
        """Stop and join the batching worker; repeated calls are harmless."""

        if self._worker.is_alive():
            self._requests.put(self._STOP)
            self._worker.join(timeout=5)

    def _run(self) -> None:
        while True:
            first = self._requests.get()
            if first is self._STOP:
                return
            assert isinstance(first, _EncodeRequest)
            batch = [first]
            text_count = len(first.texts)
            deadline = time.monotonic() + self.max_wait_seconds
            stop_after_batch = False
            while text_count < self.max_batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    candidate = self._requests.get(timeout=remaining)
                except queue.Empty:
                    break
                if candidate is self._STOP:
                    stop_after_batch = True
                    break
                assert isinstance(candidate, _EncodeRequest)
                if text_count + len(candidate.texts) > self.max_batch_size:
                    # Preserve FIFO order without exceeding the pinned batch.
                    self._requests.put(candidate)
                    break
                batch.append(candidate)
                text_count += len(candidate.texts)

            try:
                vectors = self.delegate.encode(
                    [text for request in batch for text in request.texts]
                )
                offset = 0
                for request in batch:
                    end = offset + len(request.texts)
                    request.result = vectors[offset:end]
                    offset = end
            except BaseException as exc:  # propagate delegate failures verbatim
                for request in batch:
                    request.error = exc
            finally:
                for request in batch:
                    request.completed.set()
            if stop_after_batch:
                return
