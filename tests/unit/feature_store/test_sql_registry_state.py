from __future__ import annotations

import argparse
import json

import pytest
from sqlalchemy import create_engine, select

from feast.infra.registry.sql import metadata as feast_registry_metadata
from recsys_feature_store_runtime.sql_registry_state import (
    build_parser,
    build_registry_url,
    configure_registry_url,
    restore_project,
    snapshot_project,
    verify_project,
)


def test_sql_registry_snapshot_restore_is_project_scoped(tmp_path, monkeypatch) -> None:
    registry_url = f"sqlite:///{tmp_path / 'registry.db'}"
    monkeypatch.setenv("FEAST_SQL_REGISTRY_URL", registry_url)
    engine = create_engine(registry_url)
    feast_registry_metadata.create_all(engine)
    projects = feast_registry_metadata.tables["projects"]
    entities = feast_registry_metadata.tables["entities"]

    with engine.begin() as connection:
        connection.execute(
            projects.insert(),
            {
                "project_id": "recsys",
                "project_name": "recsys",
                "last_updated_timestamp": 1,
                "project_proto": b"project-before",
            },
        )
        connection.execute(
            entities.insert(),
            [
                {
                    "entity_name": "user",
                    "project_id": "recsys",
                    "last_updated_timestamp": 1,
                    "entity_proto": b"user-before",
                },
                {
                    "entity_name": "other",
                    "project_id": "other-project",
                    "last_updated_timestamp": 1,
                    "entity_proto": b"other-before",
                },
            ],
        )

    state = snapshot_project("recsys", image_reference="registry/image@sha256:test")
    with engine.begin() as connection:
        connection.execute(
            entities.update()
            .where(entities.c.project_id == "recsys")
            .values(entity_proto=b"user-after")
        )

    restore_project(state)
    with engine.connect() as connection:
        recsys_proto = connection.execute(
            select(entities.c.entity_proto).where(entities.c.project_id == "recsys")
        ).scalar_one()
        other_proto = connection.execute(
            select(entities.c.entity_proto).where(
                entities.c.project_id == "other-project"
            )
        ).scalar_one()

    assert state["imageReference"] == "registry/image@sha256:test"
    assert recsys_proto == b"user-before"
    assert other_proto == b"other-before"


def test_registry_url_uses_offline_postgres_connection(monkeypatch) -> None:
    monkeypatch.delenv("FEAST_SQL_REGISTRY_URL", raising=False)
    monkeypatch.setenv("FEAST_POSTGRES_HOST", "postgres.internal")
    monkeypatch.setenv("FEAST_POSTGRES_PORT", "5544")
    monkeypatch.setenv("FEAST_POSTGRES_DB", "offline")
    monkeypatch.setenv("FEAST_POSTGRES_SCHEMA", "feast_registry")
    monkeypatch.setenv("FEAST_POSTGRES_USER", "registry")
    monkeypatch.setenv("FEAST_POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("FEAST_POSTGRES_SSLMODE", "require")

    registry_url = configure_registry_url()

    assert registry_url.startswith(
        "postgresql+psycopg2://registry:secret@postgres.internal:5544/offline?"
    )
    assert "search_path%3Dfeast_registry" in registry_url
    assert "sslmode=require" in registry_url
    assert build_registry_url() == registry_url


def _seed_complete_registry(registry_url: str) -> None:
    engine = create_engine(registry_url)
    feast_registry_metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            feast_registry_metadata.tables["projects"].insert(),
            {
                "project_id": "recsys",
                "project_name": "recsys",
                "last_updated_timestamp": 1,
                "project_proto": b"project",
            },
        )
        connection.execute(
            feast_registry_metadata.tables["entities"].insert(),
            {
                "entity_name": "user",
                "project_id": "recsys",
                "last_updated_timestamp": 1,
                "entity_proto": b"user",
            },
        )
        connection.execute(
            feast_registry_metadata.tables["feature_views"].insert(),
            {
                "feature_view_name": "user_features",
                "project_id": "recsys",
                "last_updated_timestamp": 1,
                "materialized_intervals": None,
                "feature_view_proto": b"view",
                "user_metadata": None,
            },
        )
        connection.execute(
            feast_registry_metadata.tables["feature_services"].insert(),
            {
                "feature_service_name": "ranking",
                "project_id": "recsys",
                "last_updated_timestamp": 1,
                "feature_service_proto": b"service",
            },
        )


def test_verify_project_rejects_missing_or_incomplete_registry(
    tmp_path, monkeypatch
) -> None:
    registry_url = f"sqlite:///{tmp_path / 'registry.db'}"
    monkeypatch.setenv("FEAST_SQL_REGISTRY_URL", registry_url)
    with pytest.raises(RuntimeError, match="tables are missing"):
        verify_project()

    feast_registry_metadata.create_all(create_engine(registry_url))
    with pytest.raises(RuntimeError, match="project is incomplete"):
        verify_project()

    _seed_complete_registry(registry_url)
    assert verify_project() == {
        "projects": 1,
        "entities": 1,
        "feature_views": 1,
        "feature_services": 1,
    }


def test_cli_handlers_write_restore_verify_and_print(
    tmp_path, monkeypatch, capsys
) -> None:
    registry_url = f"sqlite:///{tmp_path / 'registry.db'}"
    monkeypatch.setenv("FEAST_SQL_REGISTRY_URL", registry_url)
    _seed_complete_registry(registry_url)
    parser = build_parser()
    state_path = tmp_path / "state.json"

    snapshot_args = parser.parse_args(
        [
            "snapshot",
            "--project",
            "recsys",
            "--image-reference",
            "image@sha256:test",
            "--output",
            str(state_path),
        ]
    )
    snapshot_args.handler(snapshot_args)
    assert json.loads(state_path.read_text())["imageReference"] == "image@sha256:test"

    print_args = argparse.Namespace(project="recsys", image_reference="", output=None)
    parser.parse_args(["snapshot"]).handler(print_args)
    assert '"project": "recsys"' in capsys.readouterr().out

    engine = create_engine(registry_url)
    feature_views = feast_registry_metadata.tables["feature_views"]
    with engine.begin() as connection:
        connection.execute(feature_views.delete())
    restore_args = parser.parse_args(["restore", "--state-path", str(state_path)])
    restore_args.handler(restore_args)

    verify_args = parser.parse_args(["verify", "--project", "recsys"])
    verify_args.handler(verify_args)
    assert '"feature_views": 1' in capsys.readouterr().out

    url_args = parser.parse_args(["url"])
    url_args.handler(url_args)
    assert registry_url in capsys.readouterr().out
