from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

import boto3

ValidationStatus = Literal["SUCCESS", "FAILURE", "ERROR"]


def check(name: str, status: str, expected: Any, observed: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "expected": expected,
        "observed": observed,
    }


def dataset_result(checks: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {item["status"] for item in checks}
    status = (
        "ERROR"
        if "ERROR" in statuses
        else "FAILURE"
        if "FAILURE" in statuses
        else "SUCCESS"
    )
    return {"status": status, "checks": checks}


@dataclass(frozen=True)
class DatasetValidationResult:
    dataset_key: str
    status: ValidationStatus
    checks: tuple[dict[str, object], ...] = ()
    properties: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationReport:
    schema_version: int
    product_id: str
    run_id: str
    generated_at: str
    datasets: tuple[DatasetValidationResult, ...]
    report_uri: str = ""


def validation_report(
    product_id: str,
    run_id: str,
    datasets: Mapping[str, Mapping[str, object]],
    *,
    report_uri: str = "",
) -> ValidationReport:
    return ValidationReport(
        schema_version=1,
        product_id=product_id,
        run_id=run_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        datasets=tuple(
            DatasetValidationResult(
                dataset_key=key,
                status=str(value.get("status", "ERROR")),  # type: ignore[arg-type]
                checks=tuple(value.get("checks", ())),  # type: ignore[arg-type]
                properties={
                    str(name): str(item)
                    for name, item in dict(value.get("properties", {})).items()  # type: ignore[arg-type]
                },
            )
            for key, value in sorted(datasets.items())
        ),
        report_uri=report_uri,
    )


def _s3_client():
    endpoint = (
        os.getenv("MINIO_ENDPOINT")
        or os.getenv("DATA_PLATFORM_MINIO_ENDPOINT")
        or os.getenv("AWS_ENDPOINT_URL_S3")
        or None
    )
    return boto3.client("s3", endpoint_url=endpoint)


def _s3_location(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri.replace("s3a://", "s3://", 1))
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(f"Invalid S3 report URI: {uri}")
    return parsed.netloc, parsed.path.lstrip("/")


def write_validation_report(report: ValidationReport, uri: str) -> None:
    payload = asdict(report)
    payload["report_uri"] = uri
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if uri.startswith(("s3://", "s3a://")):
        bucket, key = _s3_location(uri)
        temporary_key = f"{key}.tmp-{uuid.uuid4().hex}"
        client = _s3_client()
        client.put_object(
            Bucket=bucket, Key=temporary_key, Body=body, ContentType="application/json"
        )
        try:
            client.copy_object(
                Bucket=bucket,
                Key=key,
                CopySource={"Bucket": bucket, "Key": temporary_key},
                ContentType="application/json",
                MetadataDirective="REPLACE",
            )
        finally:
            client.delete_object(Bucket=bucket, Key=temporary_key)
        return
    path = Path(uri)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary_path = Path(handle.name)
        handle.write(body)
    temporary_path.replace(path)


def read_validation_report(uri: str) -> ValidationReport:
    if uri.startswith(("s3://", "s3a://")):
        bucket, key = _s3_location(uri)
        payload = json.loads(
            _s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
        )
    else:
        payload = json.loads(Path(uri).read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(
            f"Unsupported validation report schema: {payload.get('schema_version')}"
        )
    datasets = tuple(
        DatasetValidationResult(
            dataset_key=str(item["dataset_key"]),
            status=str(item["status"]),  # type: ignore[arg-type]
            checks=tuple(item.get("checks", ())),
            properties={
                str(key): str(value)
                for key, value in item.get("properties", {}).items()
            },
        )
        for item in payload.get("datasets", ())
    )
    valid_statuses = {"SUCCESS", "FAILURE", "ERROR"}
    if any(item.status not in valid_statuses for item in datasets):
        raise ValueError("Validation report contains an unsupported status")
    return ValidationReport(
        schema_version=1,
        product_id=str(payload["product_id"]),
        run_id=str(payload["run_id"]),
        generated_at=str(payload["generated_at"]),
        datasets=datasets,
        report_uri=uri,
    )
