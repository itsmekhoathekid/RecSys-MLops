from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import requests

from lakehouse.iceberg import RAW_GENERATOR_TABLES
from metadata.governance_catalog import KAFKA_TOPIC_URNS, SOURCE_POSTGRES_URNS
from metadata.runtime_lineage import RuntimeLineageRecorder
from validate.governance_contracts import check, dataset_result, write_report


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
        "table.include.list": os.getenv("DEBEZIUM_TABLE_INCLUDE_LIST", TABLE_INCLUDE_LIST),
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


def register_connector(name: str, config: dict[str, Any]) -> dict[str, Any]:
    response = requests.put(
        f"{connect_url()}/connectors/{name}/config",
        headers={"Content-Type": "application/json"},
        data=json.dumps(config),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description="Register K8s data-platform Kafka Connect connectors.")
    parser.add_argument("--connector", choices=sorted(CONNECTORS), required=True)
    parser.add_argument("--wait-timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    name, config_factory = CONNECTORS[args.connector]
    with RuntimeLineageRecorder("CDC_INGESTION", "register_debezium_connector") as lineage:
        wait_for_connect(timeout_seconds=args.wait_timeout_seconds)
        config = config_factory()
        result = register_connector(name, config)
        included_tables = {
            item.rsplit(".", 1)[-1]
            for item in str(config.get("table.include.list", "")).split(",")
            if item.strip()
        }
        actual_tables = sorted(included_tables.intersection(RAW_GENERATOR_TABLES))
        lineage.add_inputs(*(SOURCE_POSTGRES_URNS[table] for table in actual_tables))
        lineage.add_outputs(*(KAFKA_TOPIC_URNS[table] for table in actual_tables))
        datasets: dict[str, dict[str, Any]] = {}
        for table in RAW_GENERATOR_TABLES:
            status = "SUCCESS" if table in included_tables else "FAILURE"
            source_check = check("connector_source_mapping", status, f"public.{table}", sorted(included_tables))
            topic_check = check("connector_topic_mapping", status, f"cdc.{table}", f"cdc.{table}" if status == "SUCCESS" else None)
            datasets[SOURCE_POSTGRES_URNS[table]] = dataset_result([source_check])
            datasets[KAFKA_TOPIC_URNS[table]] = dataset_result([topic_check])
        report = write_report("CDC_INGESTION", datasets)
        if report["status"] != "SUCCESS":
            lineage.fail(f"CDC connector data contract status: {report['status']}")
            raise RuntimeError(f"CDC connector contract failed: {report}")
        print(json.dumps({"name": name, "result": result, "contract": report["status"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
