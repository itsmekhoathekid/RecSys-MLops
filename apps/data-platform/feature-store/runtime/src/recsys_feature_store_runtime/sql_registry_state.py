from __future__ import annotations

"""SQL-backed Feast registry configuration and state management."""

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, inspect, select
from sqlalchemy.engine import URL


DEFAULT_PROJECT = "recsys"


def _registry_metadata() -> Any:
    """Load Feast's SQLAlchemy metadata only for registry state operations."""
    from feast.infra.registry.sql import metadata

    return metadata


def build_registry_url() -> str:
    configured = os.getenv("FEAST_SQL_REGISTRY_URL", "").strip()
    if configured:
        return configured

    schema = os.getenv("FEAST_POSTGRES_SCHEMA", "feature_store")
    url = URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("FEAST_POSTGRES_USER", "feast"),
        password=os.getenv("FEAST_POSTGRES_PASSWORD", "feast"),
        host=os.getenv(
            "FEAST_POSTGRES_HOST",
            "feature-postgres.recsys-dataflow.svc.cluster.local",
        ),
        port=int(os.getenv("FEAST_POSTGRES_PORT", "5432")),
        database=os.getenv("FEAST_POSTGRES_DB", "feature_store"),
        query={
            "sslmode": os.getenv("FEAST_POSTGRES_SSLMODE", "disable"),
            "options": f"-csearch_path={schema}",
        },
    )
    return url.render_as_string(hide_password=False)


def configure_registry_url() -> str:
    url = build_registry_url()
    os.environ["FEAST_SQL_REGISTRY_URL"] = url
    return url


def _encode_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"encoding": "base64", "value": base64.b64encode(value).decode("ascii")}
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and value.get("encoding") == "base64":
        return base64.b64decode(value["value"])
    return value


def snapshot_project(
    project: str = DEFAULT_PROJECT,
    *,
    image_reference: str = "",
) -> dict[str, Any]:
    registry_metadata = _registry_metadata()
    engine = create_engine(configure_registry_url())
    inspector = inspect(engine)
    tables: dict[str, Any] = {}
    with engine.connect() as connection:
        for table in registry_metadata.sorted_tables:
            existed = inspector.has_table(table.name)
            rows: list[dict[str, Any]] = []
            if existed and "project_id" in table.c:
                rows = [
                    {key: _encode_value(value) for key, value in row.items()}
                    for row in connection.execute(
                        select(table).where(table.c.project_id == project)
                    ).mappings()
                ]
            tables[table.name] = {"existed": existed, "rows": rows}
    engine.dispose()
    return {
        "formatVersion": 1,
        "project": project,
        "imageReference": image_reference,
        "tables": tables,
    }


def restore_project(state: dict[str, Any]) -> None:
    registry_metadata = _registry_metadata()
    project = str(state["project"])
    engine = create_engine(configure_registry_url())
    registry_metadata.create_all(engine)
    table_state = state.get("tables", {})

    with engine.begin() as connection:
        for table in reversed(registry_metadata.sorted_tables):
            if "project_id" in table.c:
                connection.execute(delete(table).where(table.c.project_id == project))
        for table in registry_metadata.sorted_tables:
            rows = table_state.get(table.name, {}).get("rows", [])
            if rows:
                connection.execute(
                    table.insert(),
                    [
                        {key: _decode_value(value) for key, value in row.items()}
                        for row in rows
                    ],
                )
    engine.dispose()


def verify_project(project: str = DEFAULT_PROJECT) -> dict[str, int]:
    registry_metadata = _registry_metadata()
    engine = create_engine(configure_registry_url())
    inspector = inspect(engine)
    required_tables = ("projects", "entities", "feature_views", "feature_services")
    missing = [name for name in required_tables if not inspector.has_table(name)]
    if missing:
        raise RuntimeError(
            f"Feast SQL registry tables are missing: {', '.join(missing)}"
        )

    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for name in required_tables:
            table = registry_metadata.tables[name]
            counts[name] = len(
                connection.execute(
                    select(table.c.project_id).where(table.c.project_id == project)
                ).all()
            )
    engine.dispose()
    if counts["projects"] != 1 or counts["entities"] < 1 or counts["feature_views"] < 1:
        raise RuntimeError(f"Feast SQL registry project is incomplete: {counts}")
    return counts


def _write_snapshot(args: argparse.Namespace) -> None:
    payload = snapshot_project(args.project, image_reference=args.image_reference)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


def _restore_snapshot(args: argparse.Namespace) -> None:
    restore_project(json.loads(Path(args.state_path).read_text(encoding="utf-8")))


def _verify(args: argparse.Namespace) -> None:
    print(json.dumps(verify_project(args.project), sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage atomic Feast SQL registry state"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("url").set_defaults(
        handler=lambda _args: print(build_registry_url())
    )

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--project", default=DEFAULT_PROJECT)
    snapshot.add_argument("--image-reference", default="")
    snapshot.add_argument("--output")
    snapshot.set_defaults(handler=_write_snapshot)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--state-path", required=True)
    restore.set_defaults(handler=_restore_snapshot)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--project", default=DEFAULT_PROJECT)
    verify.set_defaults(handler=_verify)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
