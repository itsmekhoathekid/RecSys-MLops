from __future__ import annotations

import argparse
import json
import math
import os
import time
from typing import Any

import requests

from lakehouse.iceberg import RAW_GENERATOR_TABLES
from validate.governance_contracts import build_validation_report, check, dataset_result


TABLE_INCLUDE_LIST = (
    "public.users,public.user_preferences,public.products,public.product_snapshots,"
    "public.sessions,public.recommendation_requests,public.impressions,"
    "public.behavior_events,public.orders,public.order_items"
)


def connect_url() -> str:
    return os.getenv("KAFKA_CONNECT_URL", "http://kafka-connect:8083").rstrip("/")


def debezium_config() -> dict[str, Any]:
    config = {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "database.hostname": os.getenv("POSTGRES_HOST", "source-postgres"),
        "database.port": os.getenv("POSTGRES_PORT", "5432"),
        "database.user": os.getenv("POSTGRES_USER", "recsys"),
        "database.password": os.getenv("POSTGRES_PASSWORD", "recsys"),
        "database.dbname": os.getenv("POSTGRES_DB", "recsys"),
        "topic.prefix": "cdc",
        "plugin.name": "pgoutput",
        "slot.name": os.getenv("DEBEZIUM_SLOT_NAME", "recsys_slot"),
        "publication.autocreate.mode": "filtered",
        "table.include.list": os.getenv(
            "DEBEZIUM_TABLE_INCLUDE_LIST", TABLE_INCLUDE_LIST
        ),
        "tombstones.on.delete": "false",
        "include.schema.changes": "false",
        "transforms": "route",
        "transforms.route.type": "org.apache.kafka.connect.transforms.RegexRouter",
        "transforms.route.regex": r"cdc\.public\.([^.]+)",
        "transforms.route.replacement": r"cdc.$1",
    }
    snapshot_mode = os.getenv("DEBEZIUM_SNAPSHOT_MODE")
    if snapshot_mode:
        config["snapshot.mode"] = snapshot_mode
    return config


CONNECTORS = {
    "debezium": ("recsys-postgres-cdc", debezium_config),
}


def wait_for_connect(timeout_seconds: int = 180, poll_seconds: int = 5) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() <= deadline:
        try:
            requests.get(f"{connect_url()}/connectors", timeout=5).raise_for_status()
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(poll_seconds)
    raise SystemExit(f"Kafka Connect not ready at {connect_url()}: {last_error}")


def _retryable_status(status_code: int) -> bool:
    return status_code in {404, 409, 423, 429} or status_code >= 500


def register_connector(
    name: str,
    config: dict[str, Any],
    *,
    timeout_seconds: int,
    poll_seconds: int = 5,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | str | None = None
    while time.monotonic() <= deadline:
        try:
            response = requests.put(
                f"{connect_url()}/connectors/{name}/config",
                headers={"Content-Type": "application/json"},
                data=json.dumps(config),
                timeout=30,
            )
        except requests.RequestException as exc:
            last_error = exc
        else:
            status_code = int(getattr(response, "status_code", 200))
            if _retryable_status(status_code):
                last_error = f"HTTP {status_code}"
            else:
                response.raise_for_status()
                return response.json()
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"Kafka Connect did not accept connector config: {name}; "
        f"last_error={last_error}"
    )


def wait_for_connector_running(
    name: str,
    *,
    timeout_seconds: int,
    poll_seconds: int = 5,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_states: dict[str, Any] = {}
    last_error: Exception | str | None = None
    while time.monotonic() <= deadline:
        try:
            response = requests.get(
                f"{connect_url()}/connectors/{name}/status",
                timeout=5,
            )
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(poll_seconds)
            continue
        else:
            status_code = int(getattr(response, "status_code", 200))
            if _retryable_status(status_code):
                last_error = f"HTTP {status_code}"
                time.sleep(poll_seconds)
                continue
            response.raise_for_status()
        status = response.json()
        connector_state = status.get("connector", {}).get("state")
        task_states = [task.get("state") for task in status.get("tasks", [])]
        last_states = {
            "connector": connector_state,
            "tasks": task_states,
        }
        if (
            connector_state == "RUNNING"
            and task_states
            and all(state == "RUNNING" for state in task_states)
        ):
            return last_states
        # PUT restarts an existing failed task asynchronously. Exiting on the
        # stale FAILED status makes Kubernetes repeat the PUT and continuously
        # resets Kafka Connect's recovery window.
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"Kafka Connect connector did not become RUNNING: {name}; "
        f"states={last_states}; last_error={last_error}"
    )


def remaining_seconds(deadline: float) -> int:
    return max(1, math.ceil(deadline - time.monotonic()))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register K8s data-platform Kafka Connect connectors."
    )
    parser.add_argument("--connector", choices=sorted(CONNECTORS), required=True)
    parser.add_argument("--wait-timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    name, config_factory = CONNECTORS[args.connector]
    deadline = time.monotonic() + args.wait_timeout_seconds
    wait_for_connect(timeout_seconds=remaining_seconds(deadline))
    config = config_factory()
    register_connector(name, config, timeout_seconds=remaining_seconds(deadline))
    connector_status = wait_for_connector_running(
        name, timeout_seconds=remaining_seconds(deadline)
    )
    included_tables = {
        item.rsplit(".", 1)[-1]
        for item in str(config.get("table.include.list", "")).split(",")
        if item.strip()
    }
    datasets: dict[str, dict[str, Any]] = {}
    for table in RAW_GENERATOR_TABLES:
        status = "SUCCESS" if table in included_tables else "FAILURE"
        source_check = check(
            "connector_source_mapping", status, f"public.{table}", sorted(included_tables)
        )
        topic_check = check(
            "connector_topic_mapping", status, f"cdc.{table}",
            f"cdc.{table}" if status == "SUCCESS" else None,
        )
        datasets[f"source_postgres.public.{table}"] = dataset_result([source_check])
        datasets[f"kafka.cdc.{table}"] = dataset_result([topic_check])
    report = build_validation_report("CDC_INGESTION", datasets)
    if report["status"] != "SUCCESS":
        raise RuntimeError(f"CDC connector contract failed: {report}")
    print(json.dumps(
        {"name": name, "status": connector_status, "contract": report["status"]},
        indent=2, sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
