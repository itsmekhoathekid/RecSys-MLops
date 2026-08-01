from __future__ import annotations

import importlib

import pytest

policy = importlib.import_module("jenkins.python.migration_policy")


def test_destructive_sql_patterns_are_rejected():
    assert policy.DESTRUCTIVE_SQL.search("ALTER TABLE events DROP COLUMN payload")
    assert policy.DESTRUCTIVE_SQL.search("DROP TABLE events")
    assert not policy.DESTRUCTIVE_SQL.search("ALTER TABLE events ADD COLUMN trace_id text")


def test_reversible_policy_requires_all_compensation_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "ROOT", tmp_path)
    manifest_dir = tmp_path / "jenkins/config/migrations"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "api.json").write_text('{"up":"x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing executable fields"):
        policy.validate_reversible_manifest("api")
