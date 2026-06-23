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


def cdc_topic_names() -> list[str]:
    return [f"cdc.{table.split('.')[-1]}" for table in TABLE_INCLUDE_LIST.split(",")]


def connect_url() -> str:
    return os.getenv("KAFKA_CONNECT_URL", "http://kafka-connect:8083").rstrip("/")


def debezium_config() -> dict[str, Any]:
    return {
        "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
        "database.hostname": os.getenv("POSTGRES_HOST", "source-postgres"),
        "database.port": os.getenv("POSTGRES_PORT", "5432"),
        "database.user": os.getenv("POSTGRES_USER", "recsys"),
        "database.password": os.getenv("POSTGRES_PASSWORD", "recsys"),
        "database.dbname": os.getenv("POSTGRES_DB", "recsys"),
        "topic.prefix": "cdc",
        "plugin.name": "pgoutput",
        "slot.name": "recsys_slot",
        "publication.autocreate.mode": "filtered",
        "table.include.list": TABLE_INCLUDE_LIST,
        "tombstones.on.delete": "false",
        "include.schema.changes": "false",
        "transforms": "route",
        "transforms.route.type": "org.apache.kafka.connect.transforms.RegexRouter",
        "transforms.route.regex": r"cdc\.public\.([^.]+)",
        "transforms.route.replacement": r"cdc.$1",
    }


def s3_sink_config() -> dict[str, Any]:
    return {
        "connector.class": "io.confluent.connect.s3.S3SinkConnector",
        "topics.regex": r"cdc\..*",
        "s3.bucket.name": os.getenv("LAKE_BUCKET", "recsys-lake"),
        "s3.region": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
        "store.url": os.getenv("MINIO_ENDPOINT", "http://data-platform-minio:9000"),
        "aws.access.key.id": os.getenv("MINIO_ROOT_USER", os.getenv("AWS_ACCESS_KEY_ID", "minio")),
        "aws.secret.access.key": os.getenv("MINIO_ROOT_PASSWORD", os.getenv("AWS_SECRET_ACCESS_KEY", "minio123")),
        "storage.class": "io.confluent.connect.s3.storage.S3Storage",
        "format.class": "io.confluent.connect.s3.format.json.JsonFormat",
        "flush.size": "1",
        "topics.dir": "bronze/kafka",
        "partitioner.class": "io.confluent.connect.storage.partitioner.TimeBasedPartitioner",
        "partition.duration.ms": "86400000",
        "path.format": "'event_date='YYYY-MM-dd",
        "timestamp.extractor": "Wallclock",
        "locale": "en-US",
        "timezone": "UTC",
        "schema.compatibility": "NONE",
        "consumer.override.auto.offset.reset": "earliest",
        "consumer.override.metadata.max.age.ms": "5000",
    }


CONNECTORS = {
    "debezium": ("recsys-postgres-cdc", debezium_config),
    "s3-sink": ("recsys-kafka-minio-raw-sink", s3_sink_config),
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


def ensure_cdc_topics() -> list[str]:
    from kafka.admin import KafkaAdminClient, NewTopic
    from kafka.errors import TopicAlreadyExistsError

    bootstrap_servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:29092")
    topics = cdc_topic_names()
    admin = KafkaAdminClient(bootstrap_servers=bootstrap_servers, client_id="recsys-connector-registrar")
    try:
        existing = set(admin.list_topics())
        missing = [
            NewTopic(name=topic, num_partitions=1, replication_factor=1)
            for topic in topics
            if topic not in existing
        ]
        if missing:
            try:
                admin.create_topics(missing, validate_only=False)
            except TopicAlreadyExistsError:
                pass
    finally:
        admin.close()
    return topics


def main() -> int:
    parser = argparse.ArgumentParser(description="Register K8s data-platform Kafka Connect connectors.")
    parser.add_argument("--connector", choices=sorted(CONNECTORS), required=True)
    parser.add_argument("--wait-timeout-seconds", type=int, default=180)
    args = parser.parse_args()
    wait_for_connect(timeout_seconds=args.wait_timeout_seconds)
    name, config_factory = CONNECTORS[args.connector]
    ensured_topics = ensure_cdc_topics() if args.connector == "s3-sink" else []
    result = register_connector(name, config_factory())
    print(json.dumps({"ensured_topics": ensured_topics, "name": name, "result": result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
