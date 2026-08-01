from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from datahub.ingestion.graph.client import DataHubGraph, DatahubClientConfig

from metadata.governance_catalog import assertion_urn


LOGGER = logging.getLogger(__name__)


def validation_run_id() -> str:
    return (
        os.getenv("VALIDATION_RUN_ID")
        or os.getenv("AIRFLOW_CTX_DAG_RUN_ID")
        or datetime.now(timezone.utc).strftime("manual-%Y%m%dT%H%M%SZ")
    )


def _enabled() -> bool:
    return os.getenv("DATAHUB_VALIDATION_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _strict() -> bool:
    return os.getenv("DATAHUB_VALIDATION_STRICT", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def datahub_graph() -> DataHubGraph:
    token = (os.getenv("DATAHUB_TOKEN") or os.getenv("DATAHUB_GMS_TOKEN") or "").strip()
    return DataHubGraph(
        DatahubClientConfig(
            server=os.getenv("DATAHUB_GMS_URL", "http://localhost:8088").rstrip("/"),
            token=token or None,
            timeout_sec=30,
            retry_status_codes=[408, 425, 429, *range(500, 600)],
            retry_max_times=3,
            datahub_component="recsys-native-validation",
        )
    )


def _schema_status(checks: list[dict[str, Any]], fallback: str) -> str:
    statuses = [
        str(check.get("status", "ERROR"))
        for check in checks
        if check.get("name") in {"required_columns", "schema", "table_read"}
    ]
    if not statuses:
        return fallback
    if "ERROR" in statuses:
        return "ERROR"
    if "FAILURE" in statuses:
        return "FAILURE"
    return "SUCCESS"


def _report_result(
    graph: DataHubGraph,
    *,
    urn: str,
    status: str,
    pipeline: str,
    run_id: str,
    checks: list[dict[str, Any]],
) -> None:
    attempts = max(1, int(os.getenv("DATAHUB_VALIDATION_MAX_ATTEMPTS", "6")))
    retry_delay = max(
        0.0, float(os.getenv("DATAHUB_VALIDATION_RETRY_DELAY_SECONDS", "1"))
    )
    for attempt in range(1, attempts + 1):
        try:
            graph.report_assertion_result(
                urn=urn,
                timestamp_millis=int(time.time() * 1000),
                type=status,
                properties=[
                    {"key": "pipeline", "value": pipeline},
                    {"key": "run_id", "value": run_id},
                    {
                        "key": "observed_checks",
                        "value": json.dumps(checks, sort_keys=True, default=str),
                    },
                ],
            )
            return
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(retry_delay)


def publish_validation_results(
    pipeline: str,
    datasets: dict[str, dict[str, Any]],
    *,
    run_id: str | None = None,
    graph: DataHubGraph | None = None,
) -> dict[str, Any]:
    run_id = run_id or validation_run_id()
    statuses = {result.get("status", "ERROR") for result in datasets.values()}
    overall_status = (
        "ERROR"
        if "ERROR" in statuses
        else "FAILURE"
        if "FAILURE" in statuses
        else "SUCCESS"
    )
    payload: dict[str, Any] = {
        "pipeline": pipeline,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": overall_status,
        "datasets": datasets,
        "datahub": {"mode": "custom-assertion-writeback", "published": False},
    }
    if not _enabled():
        payload["datahub"]["reason"] = "disabled"
        return payload

    own_graph = graph is None
    client = graph or datahub_graph()
    errors: list[str] = []
    published = 0
    try:
        for dataset, result in sorted(datasets.items()):
            status = str(result.get("status", "ERROR"))
            checks = list(result.get("checks", []))
            try:
                quality_urn = assertion_urn(dataset, "data_quality")
                client.upsert_custom_assertion(
                    urn=quality_urn,
                    entity_urn=dataset,
                    type="DATA_QUALITY",
                    description=f"Data quality validation for {dataset}",
                    platform_name="recsys-native-validation",
                    logic=json.dumps(
                        {"pipeline": pipeline, "checks": checks},
                        sort_keys=True,
                        default=str,
                    ),
                )
                _report_result(
                    client,
                    urn=assertion_urn(dataset, "schema"),
                    status=_schema_status(checks, status),
                    pipeline=pipeline,
                    run_id=run_id,
                    checks=checks,
                )
                _report_result(
                    client,
                    urn=quality_urn,
                    status=status,
                    pipeline=pipeline,
                    run_id=run_id,
                    checks=checks,
                )
                published += 1
            except Exception as exc:
                errors.append(f"{dataset}: {exc}")
    finally:
        if own_graph:
            client.close()

    payload["datahub"] = {
        "mode": "custom-assertion-writeback",
        "published": published == len(datasets),
        "published_datasets": published,
        "total_datasets": len(datasets),
    }
    if errors:
        payload["datahub"]["errors"] = errors
        message = "Unable to publish native DataHub validation results: " + " | ".join(
            errors
        )
        if _strict():
            raise RuntimeError(message)
        LOGGER.warning(message)
    return payload
