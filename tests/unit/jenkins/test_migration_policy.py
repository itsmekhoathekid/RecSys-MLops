from __future__ import annotations

import importlib

import pytest

policy = importlib.import_module("jenkins.python.migration_policy")


def test_destructive_sql_patterns_are_rejected():
    assert policy.DESTRUCTIVE_SQL.search("ALTER TABLE events DROP COLUMN payload")
    assert policy.DESTRUCTIVE_SQL.search("DROP TABLE events")
    assert not policy.DESTRUCTIVE_SQL.search(
        "ALTER TABLE events ADD COLUMN trace_id text"
    )


def test_operational_cutover_utilities_are_not_database_migrations(monkeypatch):
    monkeypatch.setattr(
        policy.subprocess,
        "check_output",
        lambda *args, **kwargs: (
            "ops/migrations/datahub-dataset-lineage-cutover/cutover.py\n"
            "apps/api/migrations/001_expand.sql\n"
        ),
    )

    assert policy.changed_files("base") == [
        policy.ROOT / "apps/api/migrations/001_expand.sql"
    ]


def test_reversible_policy_requires_all_compensation_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    manifest_dir = tmp_path / "jenkins/config/migrations"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "inference_api.json").write_text('{"up":"x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing executable fields"):
        policy.validate_reversible_manifest("inference_api")
