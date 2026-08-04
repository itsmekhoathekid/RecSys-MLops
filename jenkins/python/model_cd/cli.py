from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from jenkins.python.model_cd.config import write_values
from jenkins.python.model_cd.helm_release import deploy
from jenkins.python.model_cd.manifests import (
    copy_s3_prefix,
    latest_storage_uri,
    read_manifest,
    upload_manifest,
    verify_model_repository,
)
from jenkins.python.model_cd.promotion_gates import (
    assert_promote_gates,
    evaluate_candidate_gates,
)


def stage_manifests(args: argparse.Namespace) -> tuple[dict, dict | None]:
    control_uri = args.control_manifest_uri or args.manifest_uri
    candidate_uri = args.candidate_manifest_uri or args.manifest_uri
    control_manifest = read_manifest(control_uri)
    candidate_manifest = (
        read_manifest(candidate_uri)
        if args.stage in {"shadow-start", "ab-start", "ab-step", "evaluate", "promote"}
        else None
    )
    return control_manifest, candidate_manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy promoted RecSys Triton model to KServe"
    )
    parser.add_argument(
        "--manifest-uri",
        default=os.getenv(
            "PROMOTION_MANIFEST_URI",
            "s3://recsys-model-store/promotions/bst/latest.json",
        ),
    )
    parser.add_argument("--control-manifest-uri", default="")
    parser.add_argument("--candidate-manifest-uri", default="")
    parser.add_argument("--candidate-weight-percent", type=int, default=10)
    parser.add_argument("--experiment-id", default="")
    parser.add_argument(
        "--stage",
        choices=[
            "deploy",
            "shadow-start",
            "ab-start",
            "ab-step",
            "evaluate",
            "promote",
            "rollback",
        ],
        default="deploy",
    )
    parser.add_argument("--prometheus-url", default="")
    parser.add_argument("--gate-window", default="10m")
    parser.add_argument("--max-error-delta", type=float, default=0.02)
    parser.add_argument("--max-latency-ratio", type=float, default=1.5)
    parser.add_argument("--min-quality-ratio", type=float, default=0.95)
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument("--output-dir", default=".model-cd")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout", default="300s")
    args = parser.parse_args()
    requested_stage = args.stage

    manifest, candidate_manifest = stage_manifests(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    experiment_id = args.experiment_id or f"bst-{int(time.time())}"
    skip_final_verify = False
    retain_candidate = False
    cleanup_candidate_after_deploy = False
    if args.stage == "evaluate":
        decision = evaluate_candidate_gates(
            args.prometheus_url,
            args.gate_window,
            experiment_id=experiment_id,
            max_error_delta=args.max_error_delta,
            max_latency_ratio=args.max_latency_ratio,
            min_quality_ratio=args.min_quality_ratio,
            min_samples=max(0, args.min_samples),
        )
        (output_dir / "ab-decision.json").write_text(
            json.dumps(asdict(decision), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        if decision.decision == "rollback":
            candidate_manifest = None
            args.stage = "rollback"
            args.candidate_weight_percent = 0
        else:
            args.stage = "ab-step"
    if args.stage == "promote":
        if not candidate_manifest:
            raise ValueError("--candidate-manifest-uri is required for promote")
        verify_model_repository(candidate_manifest["triton_storage_uri"])
        assert_promote_gates(
            args.prometheus_url,
            args.gate_window,
            experiment_id,
            max_error_delta=args.max_error_delta,
            max_latency_ratio=args.max_latency_ratio,
            min_quality_ratio=args.min_quality_ratio,
            min_samples=max(0, args.min_samples),
        )
        serving_uri = latest_storage_uri(manifest, candidate_manifest)
        if args.apply:
            copy_s3_prefix(candidate_manifest["triton_storage_uri"], serving_uri)
            promoted_manifest = dict(candidate_manifest)
            promoted_manifest["serving_storage_uri"] = serving_uri
            promoted_manifest["promotion_manifest_uri"] = args.manifest_uri
            upload_manifest(promoted_manifest, args.manifest_uri)
        manifest = dict(candidate_manifest)
        manifest["triton_storage_uri"] = serving_uri
        manifest["serving_storage_uri"] = serving_uri
        manifest["promotion_manifest_uri"] = args.manifest_uri
        if args.apply:
            # Keep the already-ready candidate serving while the stable
            # InferenceService and API move to the promoted model. Removing it
            # in the same Helm transaction creates a DNS/not-ready gap for old
            # API pods that still carry the A/B configuration.
            retain_candidate = True
            cleanup_candidate_after_deploy = True
        else:
            candidate_manifest = None
        args.stage = "deploy"
        args.candidate_weight_percent = 0
        skip_final_verify = not args.apply
    if not skip_final_verify:
        verify_model_repository(manifest["triton_storage_uri"])
    if candidate_manifest:
        verify_model_repository(candidate_manifest["triton_storage_uri"])
    values_path = write_values(
        manifest,
        output_dir,
        control_manifest=manifest,
        candidate_manifest=candidate_manifest,
        stage=args.stage,
        candidate_weight_percent=max(0, min(100, args.candidate_weight_percent)),
        experiment_id=experiment_id,
        retain_candidate=retain_candidate,
    )
    (output_dir / "deployed-model.json").write_text(
        json.dumps(
            {
                "model_name": manifest["model_name"],
                "model_version": manifest["model_version"],
                "triton_storage_uri": manifest["triton_storage_uri"],
                "stage": args.stage,
                "candidate_model_version": (
                    candidate_manifest.get("model_version")
                    if candidate_manifest and not retain_candidate
                    else ""
                ),
                "candidate_weight_percent": (
                    args.candidate_weight_percent
                    if candidate_manifest and not retain_candidate
                    else 0
                ),
                "experiment_id": (
                    experiment_id if candidate_manifest and not retain_candidate else ""
                ),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    apply_changes = args.apply and requested_stage != "evaluate"
    if apply_changes:
        deploy(values_path, args.timeout, stage=args.stage)
        if cleanup_candidate_after_deploy:
            cleanup_values_path = write_values(
                manifest,
                output_dir,
                control_manifest=manifest,
                stage="deploy",
                candidate_weight_percent=0,
                experiment_id=experiment_id,
            )
            deploy(cleanup_values_path, args.timeout, stage="deploy")
    print(values_path)
    print(output_dir / "recsys-inference-api-values.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
