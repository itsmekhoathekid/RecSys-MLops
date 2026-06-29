from __future__ import annotations

import os
import time
from typing import Protocol

import numpy as np

from observability import observe_triton, span
from serving_utils import ab_labels


class RankerProtocol(Protocol):
    def score(self, payload: dict[str, np.ndarray]) -> tuple[list[int], list[float]]:
        ...


class TritonRanker:
    def __init__(
        self,
        url: str | None = None,
        model_name: str | None = None,
        model_version: str | None = None,
        ab_variant: str | None = None,
        ab_experiment_id: str | None = None,
    ) -> None:
        import tritonclient.grpc as grpcclient

        self.grpcclient = grpcclient
        self.client = grpcclient.InferenceServerClient(url=url or os.getenv("TRITON_URL", "localhost:8001"))
        self.model_name = model_name or os.getenv("TRITON_MODEL_NAME", "bst_ensemble")
        self.model_version = model_version or os.getenv("MODEL_VERSION", "latest")
        self.ab_variant = ab_variant
        self.ab_experiment_id = ab_experiment_id

    def score(self, payload: dict[str, np.ndarray]) -> tuple[list[int], list[float]]:
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
            with span("triton.infer", model_name=self.model_name, input_count=len(inputs)):
                result = self.client.infer(model_name=self.model_name, inputs=inputs, outputs=outputs)
            item_ids = result.as_numpy("candidate_item_id_out").astype(np.int64).reshape(-1).tolist()
            scores = result.as_numpy("score").astype(np.float32).reshape(-1).tolist()
            observe_triton(
                self.model_name,
                time.perf_counter() - start,
                labels=ab_labels(self.ab_variant, self.model_version, self.ab_experiment_id),
            )
            return item_ids, scores
        except Exception:
            observe_triton(
                self.model_name,
                time.perf_counter() - start,
                error=True,
                labels=ab_labels(self.ab_variant, self.model_version, self.ab_experiment_id),
            )
            raise
