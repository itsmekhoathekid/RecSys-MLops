from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from monitoring.pushgateway import MetricSample, push_metrics


@dataclass(frozen=True)
class RetrainResult:
    drift_run_id: str
    triggered: bool
    kfp_run_id: str | None
    reason: str
    error: str | None = None
    failed_features: list[str] | None = None
    pipeline_arguments: dict[str, Any] | None = None


def read_json(path: str) -> dict[str, Any]:
    if path.startswith("s3://"):
        import boto3

        bucket, key = path.removeprefix("s3://").split("/", 1)
        client = boto3.client(
            "s3",
            endpoint_url=os.getenv("MINIO_ENDPOINT", "http://data-platform-minio:9000"),
            aws_access_key_id=os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio")),
            aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "minio123")),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        )
        return json.loads(client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
    return json.loads(Path(path).read_text(encoding="utf-8"))


def failed_features(report: dict[str, Any]) -> list[str]:
    return [
        f"{feature.get('feature_view') or feature.get('feature_table')}.{feature.get('feature')}"
        for feature in report.get("features", [])
        if not feature.get("passed", True)
    ]


def parse_pipeline_args(values: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"--pipeline-arg must use key=value format: {value}")
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"--pipeline-arg key cannot be empty: {value}")
        parsed[key] = raw
    return parsed


def safe_run_slug(run_id: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", run_id.lower()).strip("-")
    return slug or "unknown"


def bounded_k8s_name(prefix: str, value: str, max_length: int = 47) -> str:
    slug = safe_run_slug(value)
    digest = hashlib.sha1(slug.encode("utf-8")).hexdigest()[:8]
    body_length = max(1, max_length - len(prefix) - len(digest) - 2)
    body = slug[:body_length].strip("-") or "run"
    return f"{prefix}-{body}-{digest}"


def default_pipeline_arguments(run_id: str) -> dict[str, str]:
    slug = safe_run_slug(run_id)
    base = f"/workspace/recsys/data_platform/output/retrain-{slug}"
    return {
        "pipeline_run_id": f"retrain-{run_id}",
        "output_base": base,
        "feature_summary_path": f"{base}/feature_summary.json",
        "split_output_dir": f"{base}/ml/bst_split",
        "dataset_metadata_path": f"{base}/ml/bst_split/dataset_version_meta.json",
        "ray_output_dir": f"{base}/ml/ray",
        "ray_best_result_path": f"{base}/ml/ray/best_result.json",
        "ray_status_path": f"{base}/ml/ray/rayjob_status.json",
        "eval_metrics_path": f"{base}/ml/eval_metrics.json",
        "serving_output_dir": f"{base}/ml/serving",
        "promotion_manifest_path": f"{base}/ml/serving/promotion_manifest.json",
        "ray_job_name": bounded_k8s_name("recsys-bst-ray", f"retrain-{slug}"),
    }


def push_retrain_metric(result: RetrainResult, pushgateway_url: str | None = None) -> None:
    reason = result.reason.replace("/", "_")
    samples = [
        MetricSample("recsys_ml_retrain_triggered_total", 1.0 if result.triggered else 0.0, {"reason": reason}),
        MetricSample("recsys_ml_retrain_trigger_failed_total", 1.0 if result.error else 0.0, {"reason": reason}),
    ]
    push_metrics(samples, "recsys_kubeflow_retrain", gateway_url=pushgateway_url, grouping_key={"run_id": result.drift_run_id})


def trigger_retrain(
    drift_report_path: str,
    endpoint: str,
    experiment_name: str,
    pipeline_package_path: str,
    retrain_on_drift: bool = True,
    pushgateway_url: str | None = None,
    pipeline_arguments: dict[str, Any] | None = None,
) -> RetrainResult:
    report = read_json(drift_report_path)
    run_id = str(report.get("run_id", "unknown"))
    failures = failed_features(report)
    if report.get("passed", False) or not failures:
        result = RetrainResult(run_id, False, None, "drift_passed", failed_features=failures)
        push_retrain_metric(result, pushgateway_url)
        return result
    if not retrain_on_drift:
        result = RetrainResult(run_id, False, None, "retrain_disabled", failed_features=failures)
        push_retrain_metric(result, pushgateway_url)
        return result
    arguments = default_pipeline_arguments(run_id)
    arguments.update(pipeline_arguments or {})
    try:
        import kfp

        client = kfp.Client(host=endpoint)
        experiment = client.create_experiment(name=experiment_name)
        run = client.create_run_from_pipeline_package(
            pipeline_file=pipeline_package_path,
            arguments=arguments,
            experiment_id=experiment.experiment_id,
            run_name=f"recsys-drift-retrain-{run_id}",
        )
        result = RetrainResult(run_id, True, getattr(run, "run_id", None), "feature_drift", failed_features=failures, pipeline_arguments=arguments)
    except Exception as exc:
        result = RetrainResult(run_id, False, None, "feature_drift", error=str(exc), failed_features=failures, pipeline_arguments=arguments)
    push_retrain_metric(result, pushgateway_url)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Trigger Kubeflow retraining when feature drift fails.")
    parser.add_argument("--drift-report-path", default=os.getenv("OFFLINE_FEATURE_DRIFT_REPORT_PATH", "s3://recsys-offline-feature-store/monitoring/offline_feature_drift/report.json"))
    parser.add_argument("--kfp-endpoint", default=os.getenv("KFP_ENDPOINT", "http://ml-pipeline.kubeflow.svc.cluster.local:8888"))
    parser.add_argument("--experiment-name", default=os.getenv("KFP_EXPERIMENT_NAME", "recsys-observability-retrain"))
    parser.add_argument("--pipeline-package-path", default=os.getenv("KFP_PIPELINE_PACKAGE_PATH", "/opt/recsys/infra/kubeflow/compiled/bst_training_pipeline.yaml"))
    parser.add_argument("--pushgateway-url", default=os.getenv("PUSHGATEWAY_URL", ""))
    parser.add_argument("--pipeline-arg", action="append", dest="pipeline_args", default=[])
    parser.add_argument("--disable-retrain", action="store_true")
    parser.add_argument("--fail-on-trigger-error", action="store_true")
    args = parser.parse_args()
    result = trigger_retrain(
        args.drift_report_path,
        args.kfp_endpoint,
        args.experiment_name,
        args.pipeline_package_path,
        retrain_on_drift=not args.disable_retrain and os.getenv("RETRAIN_ON_DRIFT", "true").lower() in {"1", "true", "yes"},
        pushgateway_url=args.pushgateway_url or None,
        pipeline_arguments=parse_pipeline_args(args.pipeline_args),
    )
    print(json.dumps(result.__dict__, indent=2, sort_keys=True))
    return 1 if args.fail_on_trigger_error and result.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
