from __future__ import annotations

import asyncio
import os
import time
from typing import Protocol

import numpy as np

from recsys_serving_common.concurrency import AsyncCapacityLimiter
from recsys_serving_common.observability import observe_triton, span
from recsys_inference_api.labels import ab_labels


class RankerProtocol(Protocol):
    async def score(
        self, payload: dict[str, np.ndarray]
    ) -> tuple[list[int], list[float]]: ...


class TritonRanker:
    def __init__(
        self,
        url: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        ab_variant: str | None = None,
        ab_experiment_id: str | None = None,
        timeout_seconds: float | None = None,
        max_concurrency: int | None = None,
        capacity_wait_seconds: float | None = None,
        capacity_limiter: AsyncCapacityLimiter | None = None,
    ) -> None:
        import tritonclient.grpc as grpcclient  # type: ignore[import-untyped]
        import tritonclient.grpc.aio as grpc_aio  # type: ignore[import-untyped]

        self.grpcclient = grpcclient
        self.client = grpc_aio.InferenceServerClient(
            url=url or os.getenv("TRITON_URL", "localhost:8001")
        )
        self.model_name: str = (
            model_name or os.getenv("TRITON_MODEL_NAME") or "bst_ensemble"
        )
        self.model_version: str = (
            model_version or os.getenv("MODEL_VERSION") or "latest"
        )
        self.ab_variant = ab_variant
        self.ab_experiment_id = ab_experiment_id
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("TRITON_TIMEOUT_SECONDS", "5")
        )
        self._capacity = capacity_limiter or AsyncCapacityLimiter(
            limit=max_concurrency or int(os.getenv("TRITON_MAX_CONCURRENCY", "16")),
            wait_seconds=capacity_wait_seconds
            or float(os.getenv("TRITON_CAPACITY_WAIT_SECONDS", "0.05")),
            operation="triton_inference",
        )

    async def score(
        self, payload: dict[str, np.ndarray]
    ) -> tuple[list[int], list[float]]:
        start = time.perf_counter()
        inputs = []
        for name, values in payload.items():
            infer_input = self.grpcclient.InferInput(name, values.shape, "INT64")
            infer_input.set_data_from_numpy(values)
            inputs.append(infer_input)
        outputs = [
            self.grpcclient.InferRequestedOutput("candidate_item_id_out"),
            self.grpcclient.InferRequestedOutput("score"),
        ]
        try:
            with span(
                "triton.infer", model_name=self.model_name, input_count=len(inputs)
            ):
                async with self._capacity.slot():
                    result = await asyncio.wait_for(
                        self.client.infer(
                            model_name=self.model_name,
                            inputs=inputs,
                            outputs=outputs,
                            client_timeout=self.timeout_seconds,
                        ),
                        timeout=self.timeout_seconds,
                    )
            item_ids = (
                result.as_numpy("candidate_item_id_out")
                .astype(np.int64)
                .reshape(-1)
                .tolist()
            )
            scores = result.as_numpy("score").astype(np.float32).reshape(-1).tolist()
            observe_triton(
                self.model_name,
                time.perf_counter() - start,
                labels=ab_labels(
                    self.ab_variant, self.model_version, self.ab_experiment_id
                ),
            )
            return item_ids, scores
        except Exception:
            observe_triton(
                self.model_name,
                time.perf_counter() - start,
                error=True,
                labels=ab_labels(
                    self.ab_variant, self.model_version, self.ab_experiment_id
                ),
            )
            raise

    async def aclose(self) -> None:
        await self.client.close()
