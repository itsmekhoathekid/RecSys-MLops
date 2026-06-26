from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import requests


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
    wait_for_connect(timeout_seconds=args.wait_timeout_seconds)
    name, config_factory = CONNECTORS[args.connector]
    result = register_connector(name, config_factory())
    print(json.dumps({"name": name, "result": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
