from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from openlineage.client import OpenLineageClient
from openlineage.client.event_v2 import (
    InputDataset,
    Job,
    OutputDataset,
    Run,
    RunEvent,
    RunState,
)
from openlineage.client.facet_v2 import error_message_run, tags_run
from openlineage.client.transport.http import (
    ApiKeyTokenProvider,
    HttpConfig,
    HttpTransport,
    TokenProvider,
)

from metadata.governance_catalog import ENV, dataset_urn_parts, openlineage_job_name


PRODUCER = "https://github.com/anhkhoa/RecSys-MLops"
RUN_NAMESPACE = uuid.UUID("9d15fa8c-69b4-4cc7-8699-a6765ae98691")
OPENLINEAGE_PATH = "/openapi/openlineage/api/v1/lineage"
LOGGER = logging.getLogger(__name__)


def openlineage_endpoint() -> str:
    explicit = os.getenv("DATAHUB_OPENLINEAGE_URL", "").strip()
    if explicit:
        return explicit
    gms_url = os.getenv("DATAHUB_GMS_URL", "http://localhost:8088").rstrip("/")
    return f"{gms_url}{OPENLINEAGE_PATH}"


def lineage_run_id() -> str:
    return (
        os.getenv("VALIDATION_RUN_ID")
        or os.getenv("AIRFLOW_CTX_DAG_RUN_ID")
        or datetime.now(timezone.utc).strftime("manual-%Y%m%dT%H%M%SZ")
    )


def runtime_run_uuid(pipeline: str, job_id: str, run_id: str) -> str:
    return str(uuid.uuid5(RUN_NAMESPACE, f"{pipeline}:{job_id}:{run_id}"))


def _dataset_identity(urn: str) -> tuple[str, str]:
    platform, name, env = dataset_urn_parts(urn)
    if env != ENV:
        raise ValueError(
            f"Runtime lineage dataset environment {env!r} does not match "
            f"DataHub OpenLineage environment {ENV!r}: {urn}"
        )
    return platform, name


def build_event(
    *,
    pipeline: str,
    job_id: str,
    run_id: str,
    event_type: str,
    inputs: Iterable[str] = (),
    outputs: Iterable[str] = (),
    error: str | None = None,
    event_time: str | None = None,
) -> RunEvent:
    event_type = event_type.upper()
    if event_type not in {"START", "COMPLETE", "FAIL"}:
        raise ValueError(f"Unsupported OpenLineage event type: {event_type}")
    nominal_run_id = str(run_id)
    facets = {
        "tags": tags_run.TagsRunFacet(
            tags=[
                tags_run.TagsRunFacetFields(
                    key="airflowRunId", value=nominal_run_id, source="USER"
                ),
                tags_run.TagsRunFacetFields(
                    key="pipeline", value=pipeline, source="USER"
                ),
                tags_run.TagsRunFacetFields(key="jobId", value=job_id, source="USER"),
            ]
        )
    }
    if error:
        facets["errorMessage"] = error_message_run.ErrorMessageRunFacet(
            message=error,
            programmingLanguage="python",
        )
    return RunEvent(
        eventType=RunState[event_type],
        eventTime=event_time or datetime.now(timezone.utc).isoformat(),
        run=Run(
            runId=runtime_run_uuid(pipeline, job_id, nominal_run_id),
            facets=facets,
        ),
        job=Job(
            namespace=ENV,
            name=openlineage_job_name(pipeline, job_id),
            facets={},
        ),
        inputs=[
            InputDataset(namespace=namespace, name=name, facets={})
            for namespace, name in (
                _dataset_identity(urn) for urn in sorted(set(inputs))
            )
        ],
        outputs=[
            OutputDataset(namespace=namespace, name=name, facets={})
            for namespace, name in (
                _dataset_identity(urn) for urn in sorted(set(outputs))
            )
        ],
        producer=PRODUCER,
    )


def _openlineage_client() -> OpenLineageClient:
    attempts = max(1, int(os.getenv("RUNTIME_LINEAGE_MAX_ATTEMPTS", "3")))
    timeout = max(0.1, float(os.getenv("RUNTIME_LINEAGE_HTTP_TIMEOUT_SECONDS", "5")))
    retry_delay = max(
        0.0, float(os.getenv("RUNTIME_LINEAGE_RETRY_DELAY_SECONDS", "0.5"))
    )
    token = (os.getenv("DATAHUB_TOKEN") or os.getenv("DATAHUB_GMS_TOKEN") or "").strip()
    auth = ApiKeyTokenProvider({"apiKey": token}) if token else TokenProvider({})
    config = HttpConfig(
        url=openlineage_endpoint(),
        endpoint="",
        timeout=timeout,
        auth=auth,
        retry={
            "total": attempts - 1,
            "read": attempts - 1,
            "connect": attempts - 1,
            "backoff_factor": retry_delay,
            "status_forcelist": [408, 425, 429, *range(500, 600)],
            "allowed_methods": ["POST"],
        },
    )
    return OpenLineageClient(transport=HttpTransport(config))


def emit_event(event: RunEvent) -> RunEvent:
    client = _openlineage_client()
    try:
        client.emit(event)
    finally:
        client.close()
    return event


def _lineage_enabled() -> bool:
    return os.getenv("RUNTIME_LINEAGE_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _lineage_strict() -> bool:
    return os.getenv("RUNTIME_LINEAGE_STRICT", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass
class RuntimeLineageRecorder:
    pipeline: str
    job_id: str
    inputs: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    run_id: str = field(default_factory=lineage_run_id)
    _finished: bool = field(default=False, init=False)

    def _emit(self, event_type: str, error: str | None = None) -> RunEvent | None:
        if not _lineage_enabled():
            return None
        event = build_event(
            pipeline=self.pipeline,
            job_id=self.job_id,
            run_id=self.run_id,
            event_type=event_type,
            inputs=self.inputs,
            outputs=self.outputs,
            error=error,
        )
        try:
            return emit_event(event)
        except Exception as exc:
            if _lineage_strict():
                raise
            LOGGER.warning(
                "Unable to publish %s runtime lineage for %s.%s: %s",
                event_type,
                self.pipeline,
                self.job_id,
                exc,
            )
            return None

    def add_inputs(self, *urns: str) -> None:
        self.inputs.update(urn for urn in urns if urn)

    def add_outputs(self, *urns: str) -> None:
        self.outputs.update(urn for urn in urns if urn)

    def complete(self) -> RunEvent | None:
        self._finished = True
        return self._emit("COMPLETE")

    def fail(self, error: str) -> RunEvent | None:
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
