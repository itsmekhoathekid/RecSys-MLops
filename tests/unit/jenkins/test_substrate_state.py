from __future__ import annotations

import json

import pytest

from jenkins.python.substrate_state import (
    LEGACY_QUARANTINE_ATESPACE,
    canonical_json,
    colocated_backup_key,
    migrate_v008_actor,
    redis_key_slot,
)
from ops.maintenance.migrate_substrate_v008_actor_records import parse_args


def test_legacy_actor_migration_is_deterministic_and_parseable():
    key = "actor:asr-123"
    raw = json.dumps(
        {
            "actorId": "asr-123",
            "version": "7",
            "actorTemplateNamespace": "kagent",
            "actorTemplateName": "recsys-context-agent-sandbox",
            "status": "STATUS_SUSPENDED",
            "lastSnapshot": "legacy-field-preserved-in-backup-only",
            "latestSnapshotInfo": {
                "type": "SNAPSHOT_TYPE_EXTERNAL",
                "external": {"snapshotUriPrefix": "gs://snapshot"},
            },
        }
    )
    first = migrate_v008_actor(key, raw)
    second = migrate_v008_actor(key, raw)
    assert first == second
    assert first is not None
    assert first["metadata"]["atespace"] == LEGACY_QUARANTINE_ATESPACE
    assert first["metadata"]["name"] == "asr-123"
    assert first["metadata"]["version"] == "7"
    assert first["latestSnapshotInfo"] == {
        "external": {"snapshotUriPrefix": "gs://snapshot"}
    }
    assert "actorId" not in first
    assert "lastSnapshot" not in first
    assert canonical_json(first) == canonical_json(second)


def test_migration_rejects_identity_mismatch_and_active_status():
    with pytest.raises(ValueError, match="identity does not match"):
        migrate_v008_actor(
            "actor:asr-123",
            '{"actorId":"asr-456","status":"STATUS_SUSPENDED"}',
        )
    with pytest.raises(ValueError, match="non-reviewed status"):
        migrate_v008_actor(
            "actor:asr-123",
            '{"actorId":"asr-123","status":"STATUS_RUNNING"}',
        )


def test_stale_transition_requires_an_explicit_operator_allowlist():
    raw = '{"actorId":"asr-123","status":"STATUS_SUSPENDING"}'
    with pytest.raises(ValueError, match="non-reviewed status"):
        migrate_v008_actor("actor:asr-123", raw)
    assert (
        migrate_v008_actor(
            "actor:asr-123",
            raw,
            additionally_allowed_statuses=["STATUS_SUSPENDING"],
        )["status"]
        == "STATUS_SUSPENDING"
    )


def test_current_actor_is_an_idempotent_noop_and_backup_is_colocated():
    key = "actor:asr-123"
    current = '{"metadata":{"atespace":"kagent","name":"asr-123"}}'
    assert migrate_v008_actor(key, current) is None
    backup = colocated_backup_key(key)
    assert not backup.startswith("actor:")
    assert redis_key_slot(backup) == redis_key_slot(key)


def test_apply_requires_evidence_reviewed_count_and_confirmation():
    base = ["--endpoint", "127.0.0.1:6379", "--apply"]
    with pytest.raises(SystemExit):
        parse_args(base)
    with pytest.raises(SystemExit):
        parse_args([*base, "--evidence", "evidence.json"])
    with pytest.raises(SystemExit):
        parse_args(
            [
                *base,
                "--evidence",
                "evidence.json",
                "--expected-candidate-count",
                "323",
            ]
        )
    parsed = parse_args(
        [
            *base,
            "--evidence",
            "evidence.json",
            "--expected-candidate-count",
            "323",
            "--confirm",
            "rewrite-reviewed-inactive-actors",
        ]
    )
    assert parsed.expected_candidate_count == 323
