"""Pure transformations for Substrate control-plane state migrations."""

from __future__ import annotations

import binascii
import hashlib
import json
import uuid
from collections.abc import Iterable
from typing import Any


LEGACY_ACTOR_PREFIX = "actor:asr-"
LEGACY_QUARANTINE_ATESPACE = "legacy-v0-0-8"
LEGACY_BACKUP_PREFIX = "substrate:migration:v008-v009"

_INACTIVE_STATUSES = frozenset({"STATUS_SUSPENDED", "STATUS_PAUSED"})
_PASSTHROUGH_FIELDS = (
    "actorTemplateNamespace",
    "actorTemplateName",
    "status",
    "ateomPodNamespace",
    "ateomPodName",
    "ateomPodIp",
    "inProgressSnapshot",
    "ateomPodUid",
    "workerSelector",
    "workerPoolName",
)


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

    actor_id = actor_key.removeprefix("actor:")
    desired_slot = redis_key_slot(actor_key)
    base = f"{LEGACY_BACKUP_PREFIX}:{actor_id}:"
    # A fixed-width hexadecimal suffix does not span every CRC16 residue. A
    # variable-width suffix does; one million candidates leaves ample room
    # while keeping the result deterministic.
    for nonce in range(1_000_000):
        candidate = f"{base}{nonce:x}"
        if redis_key_slot(candidate) == desired_slot:
            return candidate
    raise ValueError(f"cannot find a co-located backup key for {actor_key}")


def migrate_v008_actor(
    actor_key: str,
    raw_record: str,
    *,
    additionally_allowed_statuses: Iterable[str] = (),
) -> dict[str, Any] | None:
    """Convert one legacy Actor JSON record to the v0.0.9 metadata shape.

    ``None`` means the record already uses the current shape. The caller owns
    transport, backup, and compare-after-write concerns.
    """

    if not actor_key.startswith(LEGACY_ACTOR_PREFIX):
        raise ValueError(
            f"legacy actor key is outside the migration scope: {actor_key}"
        )
    try:
        legacy = json.loads(raw_record)
    except json.JSONDecodeError as exc:
        raise ValueError(f"legacy actor record is not JSON: {actor_key}") from exc
    if not isinstance(legacy, dict):
        raise ValueError(f"legacy actor record is not an object: {actor_key}")
    if isinstance(legacy.get("metadata"), dict):
        return None

    actor_id = legacy.get("actorId")
    if not isinstance(actor_id, str) or not actor_id:
        raise ValueError(f"legacy actor record has no actorId: {actor_key}")
    if actor_key != f"actor:{actor_id}":
        raise ValueError(f"legacy actor identity does not match its key: {actor_key}")

    status = legacy.get("status", "STATUS_UNSPECIFIED")
    allowed_statuses = _INACTIVE_STATUSES | frozenset(additionally_allowed_statuses)
    if status not in allowed_statuses:
        raise ValueError(
            f"legacy actor {actor_id} has non-reviewed status {status}; "
            "refusing migration"
        )

    atespace = legacy.get("atespace") or LEGACY_QUARANTINE_ATESPACE
    version = legacy.get("version", "1")
    migrated: dict[str, Any] = {
        "metadata": {
            "atespace": atespace,
            "name": actor_id,
            "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"substrate:v0.0.8:{actor_id}")),
            "version": str(version),
        }
    }
    for field in _PASSTHROUGH_FIELDS:
        if field in legacy:
            migrated[field] = legacy[field]

    snapshot = legacy.get("latestSnapshotInfo")
    if isinstance(snapshot, dict):
        compatible_snapshot = {
            key: snapshot[key] for key in ("external", "local") if key in snapshot
        }
        if compatible_snapshot:
            migrated["latestSnapshotInfo"] = compatible_snapshot
    return migrated
