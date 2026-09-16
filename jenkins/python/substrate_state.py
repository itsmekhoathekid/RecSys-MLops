"""Pure transformations for persisted Substrate Actor records."""

from __future__ import annotations

import binascii
import hashlib
import json
import uuid
from collections.abc import Iterable
from typing import Any


ACTOR_KEY_PREFIX = "actor:"
LEGACY_QUARANTINE_ATESPACE = "legacy-v0-0-8"
V009_BACKUP_PREFIX = "substrate:migration:v009-compat"

_INACTIVE_STATUSES = frozenset({"STATUS_SUSPENDED", "STATUS_PAUSED", "STATUS_CRASHED"})
_V009_ACTOR_FIELDS = frozenset(
    {
        "metadata",
        "actorTemplateNamespace",
        "actorTemplateName",
        "status",
        "ateomPodNamespace",
        "ateomPodName",
        "ateomPodIp",
        "inProgressSnapshot",
        "ateomPodUid",
        "latestSnapshotInfo",
        "workerSelector",
        "workerPoolName",
    }
)
_FORWARD_COMPAT_FIELDS = frozenset(
    {
        "actorVolumes",
        "inProgressSnapshotName",
        "inProgressSnapshotSourceActorVersion",
        "latestSnapshot",
        "localSnapshotInfo",
        "workerAssignment",
    }
)
_LEGACY_PASSTHROUGH_FIELDS = tuple(_V009_ACTOR_FIELDS - {"metadata"})
_WORKER_ASSIGNMENT_FIELDS = {
    "workerNamespace": "ateomPodNamespace",
    "workerPod": "ateomPodName",
    "workerPodIp": "ateomPodIp",
    "workerPodUid": "ateomPodUid",
    "workerPool": "workerPoolName",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def checksum(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def redis_key_slot(key: str) -> int:
    """Return the Redis Cluster hash slot for a UTF-8 key."""

    encoded = key.encode("utf-8")
    opening = encoded.find(b"{")
    if opening >= 0:
        closing = encoded.find(b"}", opening + 1)
        if closing > opening + 1:
            encoded = encoded[opening + 1 : closing]
    return binascii.crc_hqx(encoded, 0) % 16384


def colocated_backup_key(actor_key: str) -> str:
    """Build a deterministic backup key in the actor key's Redis hash slot."""

    actor_identity = actor_key.removeprefix(ACTOR_KEY_PREFIX)
    desired_slot = redis_key_slot(actor_key)
    base = f"{V009_BACKUP_PREFIX}:{actor_identity}:"
    for nonce in range(1_000_000):
        candidate = f"{base}{nonce:x}"
        if redis_key_slot(candidate) == desired_slot:
            return candidate
    raise ValueError(f"cannot find a co-located backup key for {actor_key}")


def _parse_record(actor_key: str, raw_record: str) -> dict[str, Any]:
    if not actor_key.startswith(ACTOR_KEY_PREFIX):
        raise ValueError(f"record is outside the Actor key scope: {actor_key}")
    try:
        record = json.loads(raw_record)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Actor record is not JSON: {actor_key}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"Actor record is not an object: {actor_key}")
    return record


def _assert_status_is_reviewed(
    actor_key: str,
    record: dict[str, Any],
    additionally_allowed_statuses: Iterable[str],
) -> None:
    status = record.get("status", "STATUS_UNSPECIFIED")
    allowed = _INACTIVE_STATUSES | frozenset(additionally_allowed_statuses)
    if status not in allowed:
        raise ValueError(
            f"Actor {actor_key} has non-reviewed status {status}; refusing migration"
        )


def _assert_metadata_identity(actor_key: str, metadata: dict[str, Any]) -> None:
    name = metadata.get("name")
    atespace = metadata.get("atespace", "")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Actor metadata has no name: {actor_key}")
    valid_keys = {f"actor:{name}"}
    if atespace:
        valid_keys.add(f"actor:{atespace}:{name}")
    if actor_key not in valid_keys:
        raise ValueError(f"Actor metadata identity does not match its key: {actor_key}")


def _migrate_metadata_record(
    actor_key: str,
    record: dict[str, Any],
    additionally_allowed_statuses: Iterable[str],
) -> dict[str, Any] | None:
    metadata = record["metadata"]
    _assert_metadata_identity(actor_key, metadata)
    extra_fields = set(record) - _V009_ACTOR_FIELDS
    if not extra_fields:
        return None
    unsupported = extra_fields - _FORWARD_COMPAT_FIELDS
    if unsupported:
        raise ValueError(
            f"Actor {actor_key} has unsupported fields: {sorted(unsupported)}"
        )
    _assert_status_is_reviewed(actor_key, record, additionally_allowed_statuses)

    migrated = {key: record[key] for key in _V009_ACTOR_FIELDS if key in record}
    assignment = record.get("workerAssignment")
    if assignment is not None:
        if not isinstance(assignment, dict):
            raise ValueError(f"Actor workerAssignment is not an object: {actor_key}")
        unsupported_assignment = set(assignment) - _WORKER_ASSIGNMENT_FIELDS.keys()
        if unsupported_assignment:
            raise ValueError(
                f"Actor {actor_key} has unsupported workerAssignment fields: "
                f"{sorted(unsupported_assignment)}"
            )
        for source, target in _WORKER_ASSIGNMENT_FIELDS.items():
            if assignment.get(source):
                migrated[target] = assignment[source]

    snapshot_name = record.get("inProgressSnapshotName")
    if snapshot_name and "inProgressSnapshot" not in migrated:
        migrated["inProgressSnapshot"] = snapshot_name
    return migrated


def _migrate_legacy_record(
    actor_key: str,
    record: dict[str, Any],
    additionally_allowed_statuses: Iterable[str],
) -> dict[str, Any]:
    actor_id = record.get("actorId")
    if not isinstance(actor_id, str) or not actor_id:
        raise ValueError(f"legacy Actor record has no actorId: {actor_key}")
    atespace = record.get("atespace") or LEGACY_QUARANTINE_ATESPACE
    valid_keys = {f"actor:{actor_id}", f"actor:{atespace}:{actor_id}"}
    if actor_key not in valid_keys:
        raise ValueError(f"legacy Actor identity does not match its key: {actor_key}")
    _assert_status_is_reviewed(actor_key, record, additionally_allowed_statuses)

    migrated: dict[str, Any] = {
        "metadata": {
            "atespace": atespace,
            "name": actor_id,
            "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"substrate:legacy:{actor_id}")),
            "version": str(record.get("version", "1")),
        }
    }
    for field in _LEGACY_PASSTHROUGH_FIELDS:
        if field in record:
            migrated[field] = record[field]

    snapshot = record.get("latestSnapshotInfo")
    if isinstance(snapshot, dict):
        compatible_snapshot = {
            key: snapshot[key] for key in ("external", "local") if key in snapshot
        }
        if compatible_snapshot:
            migrated["latestSnapshotInfo"] = compatible_snapshot
    return migrated


def migrate_actor_to_v009(
    actor_key: str,
    raw_record: str,
    *,
    additionally_allowed_statuses: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Normalize one persisted Actor to the strict Substrate v0.0.9 JSON shape.

    ``None`` means the record is already compatible. Transport, backup, and
    compare-after-write remain the caller's responsibility.
    """

    record = _parse_record(actor_key, raw_record)
    if isinstance(record.get("metadata"), dict):
        return _migrate_metadata_record(
            actor_key,
            record,
            additionally_allowed_statuses,
        )
    return _migrate_legacy_record(
        actor_key,
        record,
        additionally_allowed_statuses,
    )


def migrate_v008_actor(
    actor_key: str,
    raw_record: str,
    *,
    additionally_allowed_statuses: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Backward-compatible name for callers introduced with the first repair."""

    return migrate_actor_to_v009(
        actor_key,
        raw_record,
        additionally_allowed_statuses=additionally_allowed_statuses,
    )
