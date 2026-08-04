from __future__ import annotations

import json
from pathlib import Path


def write_values(
    manifest: dict,
    output_dir: Path,
    *,
    control_manifest: dict | None = None,
    candidate_manifest: dict | None = None,
    stage: str = "deploy",
    candidate_weight_percent: int = 0,
    experiment_id: str = "",
    retain_candidate: bool = False,
) -> Path:
    """Write release-scoped values; return the KServe artifact for compatibility."""
    control = control_manifest or manifest
    candidate = candidate_manifest
    ab_enabled = (
        stage in {"ab-start", "ab-step"}
        and candidate is not None
        and candidate_weight_percent > 0
    )
    shadow_enabled = stage == "shadow-start" and candidate is not None
    candidate_requested = candidate is not None and (
        retain_candidate or ab_enabled or shadow_enabled
    )
    metadata = {"stage": stage}
    kserve_values = {
        "modelCd": metadata,
        "kserve": {
            "enabled": True,
            "namespace": {"name": "kserve-triton-inference"},
            "secret": {"create": False},
            "inferenceService": {
                "name": "recsys-bst-triton",
                "storageUri": control["triton_storage_uri"],
                "candidateStorageUri": (
                    candidate["triton_storage_uri"] if candidate_requested else ""
                ),
                "retainCandidate": candidate_requested,
            },
        },
        "autoscaling": {"kserveResource": {"enabled": True}},
    }
    inference_values = {
        "modelCd": metadata,
        "namespace": "api-serving",
        "name": "recsys-inference-api",
        "rollout": {
            "maxSurge": 1,
            "maxUnavailable": 0,
            "minReadySeconds": 10,
            "progressDeadlineSeconds": 300,
        },
        "config": {"modelVersion": control["model_version"]},
        "autoscaling": {"minReplicas": 2},
        "abTest": {
            "enabled": ab_enabled,
            "experimentId": experiment_id,
            "candidateWeightPercent": candidate_weight_percent if ab_enabled else 0,
            "controlModelVersion": control["model_version"],
            "candidateModelVersion": candidate["model_version"] if candidate else "",
            "controlTritonUrl": (
                "recsys-bst-triton-predictor."
                "kserve-triton-inference.svc.cluster.local:9000"
            ),
            "candidateTritonUrl": (
                "recsys-bst-triton-candidate-predictor."
                "kserve-triton-inference.svc.cluster.local:9000"
                if candidate_requested
                else ""
            ),
        },
        "shadow": {
            "enabled": shadow_enabled,
            "samplePercent": 100,
            "timeoutMs": 1000,
            "queueSize": 100,
            "maxConcurrency": 4,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    kserve_target = output_dir / "recsys-kserve-values.json"
    inference_target = output_dir / "recsys-inference-api-values.json"
    kserve_target.write_text(
        json.dumps(kserve_values, indent=2, sort_keys=True), encoding="utf-8"
    )
    inference_target.write_text(
        json.dumps(inference_values, indent=2, sort_keys=True), encoding="utf-8"
    )
    return kserve_target
