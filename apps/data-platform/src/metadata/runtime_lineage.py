from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, TypeVar

from datahub.api.entities.dataprocess.dataprocess_instance import (
    DataProcessInstance,
    InstanceRunResult,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.metadata.schema_classes import (
    DataJobInputOutputClass,
    DataProcessTypeClass,
)
from datahub.metadata.urns import DataFlowUrn, DataJobUrn, DatasetUrn

from metadata.governance_catalog import job_urn, pipeline_flow_urn


RUN_NAMESPACE = uuid.UUID("9d15fa8c-69b4-4cc7-8699-a6765ae98691")
LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


def lineage_run_id() -> str:
    return (
        os.getenv("VALIDATION_RUN_ID")
        or os.getenv("AIRFLOW_CTX_DAG_RUN_ID")
        or datetime.now(timezone.utc).strftime("manual-%Y%m%dT%H%M%SZ")
    )


def runtime_run_uuid(pipeline: str, job_id: str, run_id: str) -> str:
    return str(uuid.uuid5(RUN_NAMESPACE, f"{pipeline}:{job_id}:{run_id}"))


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


def _runtime_emitter() -> DatahubRestEmitter:
    attempts = max(1, int(os.getenv("RUNTIME_LINEAGE_MAX_ATTEMPTS", "3")))
    timeout = max(0.1, float(os.getenv("RUNTIME_LINEAGE_HTTP_TIMEOUT_SECONDS", "5")))
    token = (os.getenv("DATAHUB_TOKEN") or os.getenv("DATAHUB_GMS_TOKEN") or "").strip()
    return DatahubRestEmitter(
        gms_server=os.getenv("DATAHUB_GMS_URL", "http://localhost:8088").rstrip("/"),
        token=token or None,
        timeout_sec=timeout,
        retry_status_codes=[408, 425, 429, *range(500, 600)],
        retry_methods=["POST"],
        retry_max_times=attempts - 1,
        openapi_ingestion=False,
        datahub_component="recsys-runtime-lineage",
    )


def _dataset_urns(values: set[str]) -> list[DatasetUrn]:
    return [DatasetUrn.from_string(value) for value in sorted(values)]


@dataclass
class RuntimeLineageRecorder:
    """Publish non-Airflow runtime lineage with the native DataHub SDK.

    Airflow tasks disable this recorder and are captured by the DataHub Airflow
    plugin. It remains the runtime lifecycle owner for Kafka Connect and Flink.
    """

    pipeline: str
    job_id: str
    inputs: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    run_id: str = field(default_factory=lineage_run_id)
    _finished: bool = field(default=False, init=False)
    _emitter: DatahubRestEmitter | None = field(default=None, init=False, repr=False)
    _instance: DataProcessInstance | None = field(default=None, init=False, repr=False)
    _started_at_millis: int | None = field(default=None, init=False, repr=False)

    def _ensure_runtime(self) -> tuple[DatahubRestEmitter, DataProcessInstance]:
        if self._emitter is None:
            self._emitter = _runtime_emitter()
        if self._instance is None:
            flow = DataFlowUrn.from_string(pipeline_flow_urn(self.pipeline))
            template = DataJobUrn.create_from_ids(
                data_flow_urn=str(flow),
                job_id=self.job_id,
            )
            self._instance = DataProcessInstance(
                id=runtime_run_uuid(self.pipeline, self.job_id, str(self.run_id)),
                orchestrator=flow.orchestrator,
                cluster=flow.cluster,
                type=(
                    DataProcessTypeClass.STREAMING
                    if flow.orchestrator == "flink"
                    else DataProcessTypeClass.BATCH_SCHEDULED
                ),
                template_urn=template,
                properties={
                    "nominalRunId": str(self.run_id),
                    "pipeline": self.pipeline,
                    "jobId": self.job_id,
                },
                inlets=_dataset_urns(self.inputs),
                outlets=_dataset_urns(self.outputs),
            )
        return self._emitter, self._instance

    def _sync_instance_io(self, instance: DataProcessInstance) -> None:
        instance.inlets = _dataset_urns(self.inputs)
        instance.outlets = _dataset_urns(self.outputs)

    def _emit_job_lineage(self, emitter: DatahubRestEmitter) -> None:
        emitter.emit(
            MetadataChangeProposalWrapper(
                entityUrn=job_urn(pipeline_flow_urn(self.pipeline), self.job_id),
                aspect=DataJobInputOutputClass(
                    inputDatasets=sorted(self.inputs),
                    outputDatasets=sorted(self.outputs),
                    inputDatajobs=[],
                    fineGrainedLineages=[],
                ),
            )
        )

    def _publish(self, event_name: str, operation: Callable[[], _T]) -> _T | None:
        if not _lineage_enabled():
            return None
        try:
            return operation()
        except Exception as exc:
            if _lineage_strict():
                raise
            LOGGER.warning(
                "Unable to publish %s DataHub SDK runtime lineage for %s.%s: %s",
                event_name,
                self.pipeline,
                self.job_id,
                exc,
            )
            return None

    def _publish_start(self) -> DataProcessInstance:
        emitter, instance = self._ensure_runtime()
        self._sync_instance_io(instance)
        if self.inputs or self.outputs:
            self._emit_job_lineage(emitter)
        self._started_at_millis = int(time.time() * 1000)
        instance.emit_process_start(
            emitter,
            start_timestamp_millis=self._started_at_millis,
            emit_template=False,
            materialize_iolets=False,
        )
        return instance

    def _publish_end(
        self,
        result: InstanceRunResult,
        *,
        error: str | None = None,
    ) -> DataProcessInstance:
        emitter, instance = self._ensure_runtime()
        self._sync_instance_io(instance)
        if error:
            instance.properties["error"] = error
        self._emit_job_lineage(emitter)
        for proposal in instance.generate_mcp(
            created_ts_millis=self._started_at_millis,
            materialize_iolets=False,
        ):
            emitter.emit(proposal)
        instance.emit_process_end(
            emitter,
            end_timestamp_millis=int(time.time() * 1000),
            result=result,
            result_type=instance.orchestrator,
            start_timestamp_millis=self._started_at_millis,
        )
        return instance

    def _close(self) -> None:
        if self._emitter is not None:
            try:
                self._emitter.close()
            finally:
                self._emitter = None

    def add_inputs(self, *urns: str) -> None:
        self.inputs.update(urn for urn in urns if urn)

    def add_outputs(self, *urns: str) -> None:
        self.outputs.update(urn for urn in urns if urn)

    def complete(self) -> DataProcessInstance | None:
        self._finished = True
        try:
            return self._publish(
                "SUCCESS",
                lambda: self._publish_end(InstanceRunResult.SUCCESS),
            )
        finally:
            self._close()

    def fail(self, error: str) -> DataProcessInstance | None:
        self._finished = True
        try:
            return self._publish(
                "FAILURE",
                lambda: self._publish_end(
                    InstanceRunResult.FAILURE,
                    error=error,
                ),
            )
        finally:
            self._close()

    def __enter__(self) -> RuntimeLineageRecorder:
        try:
            self._publish("STARTED", self._publish_start)
        except Exception:
            self._close()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._finished:
            return False
        if exc is None:
            self.complete()
        else:
            self.fail(str(exc))
        return False
