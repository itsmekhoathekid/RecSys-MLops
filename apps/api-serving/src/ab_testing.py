from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

from observability import METRICS
from serving_utils import ab_labels, bool_env, int_env
from triton import RankerProtocol, TritonRanker


@dataclass(frozen=True)
class TritonRoute:
    ranker: RankerProtocol
    model_version: str
    ab_variant: str | None = None
    ab_experiment_id: str | None = None


class TritonABRouter:
    def __init__(
        self,
        control_ranker: RankerProtocol,
        control_model_version: str,
        candidate_ranker: RankerProtocol | None = None,
        candidate_model_version: str | None = None,
        enabled: bool = False,
        candidate_weight_percent: int = 0,
        experiment_id: str = "",
        shadow_enabled: bool = False,
        shadow_sample_percent: int = 100,
    ) -> None:
        self.control_ranker = control_ranker
        self.control_model_version = control_model_version
        self.candidate_ranker = candidate_ranker
        self.candidate_model_version = candidate_model_version or control_model_version
        self.enabled = enabled and candidate_ranker is not None
        self.shadow_enabled = shadow_enabled and candidate_ranker is not None and not self.enabled
        self.candidate_weight_percent = max(0, min(100, candidate_weight_percent))
        self.shadow_sample_percent = max(0, min(100, shadow_sample_percent))
        self.experiment_id = experiment_id or "default"
        rollout_mode = "ab" if self.enabled else "shadow" if self.shadow_enabled else "stable"
        METRICS.set_gauge(
            "recsys_api_rollout_config_info",
            1,
            labels={
                "mode": rollout_mode,
                "experiment_id": self.experiment_id,
                "control_model_version": self.control_model_version,
                "candidate_model_version": self.candidate_model_version if self.candidate_ranker else "none",
                "candidate_weight_percent": str(self.candidate_weight_percent if self.enabled else 0),
            },
        )

    @classmethod
    def from_env(cls) -> "TritonABRouter":
        model_name = os.getenv("TRITON_MODEL_NAME", "bst_ensemble")
        control_version = os.getenv("AB_CONTROL_MODEL_VERSION") or os.getenv("MODEL_VERSION", "latest")
        candidate_version = os.getenv("AB_CANDIDATE_MODEL_VERSION", "")
        experiment_id = os.getenv("AB_EXPERIMENT_ID", "default")
        control_ranker = TritonRanker(
            url=os.getenv("AB_CONTROL_TRITON_URL") or os.getenv("TRITON_URL", "localhost:8001"),
            model_name=model_name,
            model_version=control_version,
            ab_variant="control",
            ab_experiment_id=experiment_id,
        )
        candidate_ranker: TritonRanker | None = None
        ab_enabled = bool_env("AB_TEST_ENABLED")
        shadow_enabled = bool_env("AB_SHADOW_ENABLED")
        if (ab_enabled or shadow_enabled) and os.getenv("AB_CANDIDATE_TRITON_URL"):
            candidate_ranker = TritonRanker(
                url=os.getenv("AB_CANDIDATE_TRITON_URL"),
                model_name=model_name,
                model_version=candidate_version or control_version,
                ab_variant="candidate" if ab_enabled else "shadow_candidate",
                ab_experiment_id=experiment_id,
            )
        return cls(
            control_ranker=control_ranker,
            control_model_version=control_version,
            candidate_ranker=candidate_ranker,
            candidate_model_version=candidate_version or control_version,
            enabled=ab_enabled,
            candidate_weight_percent=int_env("AB_CANDIDATE_WEIGHT_PERCENT"),
            experiment_id=experiment_id,
            shadow_enabled=shadow_enabled,
            shadow_sample_percent=int_env("AB_SHADOW_SAMPLE_PERCENT", 100),
        )

    def _bucket(self, user_id: int, mode: str) -> int:
        # Preserve the original A/B key so existing experiment assignments stay sticky.
        key_text = (
            f"{self.experiment_id}:{int(user_id)}"
            if mode == "ab"
            else f"{mode}:{self.experiment_id}:{int(user_id)}"
        )
        key = key_text.encode("utf-8")
        return int(hashlib.sha256(key).hexdigest()[:8], 16) % 100

    def assign(self, user_id: int) -> str:
        if not self.enabled or self.candidate_weight_percent <= 0:
            return "control"
        if self.candidate_weight_percent >= 100:
            return "candidate"
        bucket = self._bucket(user_id, "ab")
        return "candidate" if bucket < self.candidate_weight_percent else "control"

    def shadow_route(self, user_id: int) -> TritonRoute | None:
        if not self.shadow_enabled or self.candidate_ranker is None or self.shadow_sample_percent <= 0:
            return None
        if self.shadow_sample_percent < 100 and self._bucket(user_id, "shadow") >= self.shadow_sample_percent:
            return None
        return TritonRoute(
            ranker=self.candidate_ranker,
            model_version=self.candidate_model_version,
            ab_variant="shadow_candidate",
            ab_experiment_id=self.experiment_id,
        )

    def route(self, user_id: int) -> TritonRoute:
        variant = self.assign(user_id)
        if variant == "candidate" and self.candidate_ranker is not None:
            route = TritonRoute(
                ranker=self.candidate_ranker,
                model_version=self.candidate_model_version,
                ab_variant="candidate",
                ab_experiment_id=self.experiment_id,
            )
        else:
            route = TritonRoute(
                ranker=self.control_ranker,
                model_version=self.control_model_version,
                ab_variant="control" if (self.enabled or self.shadow_enabled) else None,
                ab_experiment_id=self.experiment_id if (self.enabled or self.shadow_enabled) else None,
            )
        METRICS.inc(
            "recsys_api_ab_assignments_total",
            labels=ab_labels(route.ab_variant, route.model_version, route.ab_experiment_id),
        )
        return route


def select_triton_route(
    ranker: RankerProtocol | TritonABRouter,
    user_id: int,
    model_version: str,
) -> TritonRoute:
    if hasattr(ranker, "route"):
        return ranker.route(user_id)  # type: ignore[union-attr]
    return TritonRoute(ranker=ranker, model_version=model_version)
