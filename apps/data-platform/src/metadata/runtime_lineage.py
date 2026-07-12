from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import pyarrow.fs as pafs


DEFAULT_LINEAGE_ROOT = "s3a://recsys-lakehouse/governance/lineage"
OPENLINEAGE_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json"
TAGS_RUN_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-0/TagsRunFacet.json"
ERROR_RUN_FACET_SCHEMA_URL = "https://openlineage.io/spec/facets/1-0-0/ErrorMessageRunFacet.json"
PRODUCER = "https://github.com/anhkhoa/RecSys-MLops"
RUN_NAMESPACE = uuid.UUID("9d15fa8c-69b4-4cc7-8699-a6765ae98691")


def _filesystem_and_path(uri: str) -> tuple[pafs.FileSystem, str]:
    value = "s3://" + uri.removeprefix("s3a://") if uri.startswith("s3a://") else uri
    parsed = urlparse(value)
    if parsed.scheme == "s3":
        endpoint_value = os.getenv("MINIO_ENDPOINT", os.getenv("DATA_PLATFORM_MINIO_ENDPOINT", "http://data-platform-minio:9000"))
        endpoint_value = endpoint_value if "://" in endpoint_value else f"http://{endpoint_value}"
        endpoint = urlparse(endpoint_value)
        return (
            pafs.S3FileSystem(
                access_key=os.getenv("AWS_ACCESS_KEY_ID", os.getenv("MINIO_ROOT_USER", "minio")),
                secret_key=os.getenv("AWS_SECRET_ACCESS_KEY", os.getenv("MINIO_ROOT_PASSWORD", "minio123")),
                region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
                scheme=endpoint.scheme,
                endpoint_override=endpoint.netloc,
            ),
            f"{parsed.netloc}{parsed.path}",
        )
    if parsed.scheme:
        raise ValueError(f"Unsupported runtime-lineage URI scheme: {parsed.scheme}")
    return pafs.LocalFileSystem(), str(Path(value))


def lineage_root() -> str:
    return os.getenv("RUNTIME_LINEAGE_ROOT", DEFAULT_LINEAGE_ROOT).rstrip("/")


def lineage_run_id() -> str:
    return (
        os.getenv("VALIDATION_RUN_ID")
        or os.getenv("AIRFLOW_CTX_DAG_RUN_ID")
        or datetime.now(timezone.utc).strftime("manual-%Y%m%dT%H%M%SZ")
    )


def runtime_run_uuid(pipeline: str, job_id: str, run_id: str) -> str:
    return str(uuid.uuid5(RUN_NAMESPACE, f"{pipeline}:{job_id}:{run_id}"))


def _safe_path(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def event_uri(
    pipeline: str,
    job_id: str,
    event_type: str,
    *,
    run_id: str,
    root: str | None = None,
) -> str:
    return (
        f"{(root or lineage_root()).rstrip('/')}/{_safe_path(pipeline.lower())}/runs/"
        f"{_safe_path(run_id)}/{_safe_path(job_id)}/{event_type.lower()}.json"
    )


def latest_event_uri(pipeline: str, job_id: str, *, root: str | None = None) -> str:
    return f"{(root or lineage_root()).rstrip('/')}/{_safe_path(pipeline.lower())}/jobs/{_safe_path(job_id)}/latest.json"


def _write_json(uri: str, payload: dict[str, Any]) -> None:
    filesystem, path = _filesystem_and_path(uri)
    parent = path.rsplit("/", 1)[0]
    filesystem.create_dir(parent, recursive=True)
    with filesystem.open_output_stream(path) as stream:
        stream.write(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))


def _read_json(uri: str) -> dict[str, Any]:
    filesystem, path = _filesystem_and_path(uri)
    with filesystem.open_input_file(path) as stream:
        return json.loads(stream.read().decode("utf-8"))


def _dataset_ref(urn: str) -> dict[str, Any]:
    return {"namespace": "datahub", "name": urn, "facets": {}}


def build_event(
    *,
    pipeline: str,
    job_id: str,
    run_id: str,
    event_type: str,
    inputs: Iterable[str] = (),
    outputs: Iterable[str] = (),
    upstream_jobs: Iterable[str] = (),
    error: str | None = None,
    event_time: str | None = None,
) -> dict[str, Any]:
    event_type = event_type.upper()
    if event_type not in {"START", "COMPLETE", "FAIL"}:
        raise ValueError(f"Unsupported OpenLineage event type: {event_type}")
    nominal_run_id = str(run_id)
    tags = [
        {"key": "airflowRunId", "value": nominal_run_id, "source": "RUNTIME"},
        {"key": "pipeline", "value": pipeline, "source": "RUNTIME"},
        {"key": "jobId", "value": job_id, "source": "RUNTIME"},
    ]
    tags.extend(
        {"key": "upstreamJob", "value": upstream_job, "source": "RUNTIME"}
        for upstream_job in sorted(set(upstream_jobs))
    )
    facets: dict[str, Any] = {
        "tags": {
            "_producer": PRODUCER,
            "_schemaURL": TAGS_RUN_FACET_SCHEMA_URL,
            "tags": tags,
        }
    }
    if error:
        facets["errorMessage"] = {
            "_producer": PRODUCER,
            "_schemaURL": ERROR_RUN_FACET_SCHEMA_URL,
            "message": error,
            "programmingLanguage": "python",
        }
    return {
        "eventType": event_type,
        "eventTime": event_time or datetime.now(timezone.utc).isoformat(),
        "run": {
            "runId": runtime_run_uuid(pipeline, job_id, nominal_run_id),
            "facets": facets,
        },
        "job": {
            "namespace": "recsys-data-platform",
            "name": f"{pipeline}.{job_id}",
            "facets": {},
        },
        "inputs": [_dataset_ref(urn) for urn in sorted(set(inputs))],
        "outputs": [_dataset_ref(urn) for urn in sorted(set(outputs))],
        "producer": PRODUCER,
        "schemaURL": OPENLINEAGE_SCHEMA_URL,
    }


def write_event(event: dict[str, Any], *, root: str | None = None) -> dict[str, Any]:
    runtime = event_runtime(event)
    pipeline = str(runtime["pipeline"])
    job_id = str(runtime["jobId"])
    run_id = str(runtime["airflowRunId"])
    event_type = str(event["eventType"])
    _write_json(event_uri(pipeline, job_id, event_type, run_id=run_id, root=root), event)
    _write_json(latest_event_uri(pipeline, job_id, root=root), event)
    return event


def read_latest_event(pipeline: str, job_id: str, *, root: str | None = None) -> dict[str, Any]:
    return _read_json(latest_event_uri(pipeline, job_id, root=root))


def event_dataset_urns(event: dict[str, Any], field_name: str) -> tuple[str, ...]:
    return tuple(str(item["name"]) for item in event.get(field_name, []) if item.get("namespace") == "datahub")


def event_runtime(event: dict[str, Any]) -> dict[str, Any]:
    tags = event["run"]["facets"]["tags"]["tags"]
    runtime: dict[str, Any] = {}
    upstream_jobs: list[str] = []
    for tag in tags:
        if tag.get("key") == "upstreamJob":
            upstream_jobs.append(str(tag.get("value", "")))
        else:
            runtime[str(tag["key"])] = tag.get("value")
    runtime["upstreamJobs"] = upstream_jobs
    return runtime


def _lineage_enabled() -> bool:
    return os.getenv("RUNTIME_LINEAGE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _lineage_strict() -> bool:
    return os.getenv("RUNTIME_LINEAGE_STRICT", "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class RuntimeLineageRecorder:
    pipeline: str
    job_id: str
    inputs: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    upstream_jobs: set[str] = field(default_factory=set)
    run_id: str = field(default_factory=lineage_run_id)
    root: str | None = None
    _finished: bool = field(default=False, init=False)

    def _emit(self, event_type: str, error: str | None = None) -> dict[str, Any] | None:
        if not _lineage_enabled():
            return None
        event = build_event(
            pipeline=self.pipeline,
            job_id=self.job_id,
            run_id=self.run_id,
            event_type=event_type,
            inputs=self.inputs,
            outputs=self.outputs,
            upstream_jobs=self.upstream_jobs,
            error=error,
        )
        try:
            return write_event(event, root=self.root)
        except Exception:
            if _lineage_strict():
                raise
            return None

    def add_inputs(self, *urns: str) -> None:
        self.inputs.update(urn for urn in urns if urn)

    def add_outputs(self, *urns: str) -> None:
        self.outputs.update(urn for urn in urns if urn)

    def complete(self) -> dict[str, Any] | None:
        self._finished = True
        return self._emit("COMPLETE")

    def fail(self, error: str) -> dict[str, Any] | None:
        self._finished = True
        return self._emit("FAIL", error)

    def __enter__(self) -> RuntimeLineageRecorder:
        self._emit("START")
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._finished:
            return False
        if exc is None:
            self.complete()
        else:
            self.fail(str(exc))
        return False
