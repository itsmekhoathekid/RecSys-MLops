#!/usr/bin/env python3
"""Back up and normalize persisted Actors for strict Substrate v0.0.9 reads."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from jenkins.python.substrate_state import (  # noqa: E402
    canonical_json,
    checksum,
    colocated_backup_key,
    migrate_actor_to_v009,
)
from ops.maintenance.valkey_resp import (  # noqa: E402
    RedisConnection,
    RedisError,
    scan_records,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-status", action="append", default=[])
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--expected-candidate-count", type=int)
    parser.add_argument("--confirm", default="")
    args = parser.parse_args(argv)
    if args.apply:
        if args.evidence is None:
            parser.error("--apply requires --evidence")
        if args.expected_candidate_count is None:
            parser.error("--apply requires --expected-candidate-count")
        if args.expected_candidate_count < 0:
            parser.error("--expected-candidate-count cannot be negative")
        if args.confirm != "rewrite-reviewed-actor-records":
            parser.error("--apply requires --confirm rewrite-reviewed-actor-records")
    return args


def collect_candidates(
    clients: Sequence[RedisConnection],
    allowed_statuses: Sequence[str],
) -> tuple[list[dict[str, Any]], int, Counter[str]]:
    candidates: list[dict[str, Any]] = []
    already_current = 0
    statuses: Counter[str] = Counter()
    seen_keys: set[str] = set()
    for client in clients:
        for key, raw in scan_records(client, "actor:*"):
            if key in seen_keys:
                raise RedisError(f"Actor key returned by multiple endpoints: {key}")
            seen_keys.add(key)
            migrated = migrate_actor_to_v009(
                key,
                raw,
                additionally_allowed_statuses=allowed_statuses,
            )
            if migrated is None:
                already_current += 1
                continue
            status = migrated.get("status", "STATUS_UNSPECIFIED")
            statuses[status] += 1
            candidates.append(
                {
                    "client": client,
                    "key": key,
                    "backupKey": colocated_backup_key(key),
                    "before": raw,
                    "after": canonical_json(migrated),
                    "status": status,
                }
            )
    return candidates, already_current, statuses


def _group_by_client(
    records: Sequence[dict[str, Any]],
) -> list[tuple[RedisConnection, list[dict[str, Any]]]]:
    grouped: dict[RedisConnection, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["client"]].append(record)
    return list(grouped.items())


def apply_candidates(records: Sequence[dict[str, Any]]) -> None:
    grouped = _group_by_client(records)
    for client, batch in grouped:
        replies = client.pipeline(
            [("SET", row["backupKey"], row["before"], "NX") for row in batch]
        )
        if any(reply not in {"OK", None} for reply in replies):
            raise RedisError(f"unexpected backup reply from {client.endpoint}")
    for client, batch in grouped:
        backups = client.pipeline([("GET", row["backupKey"]) for row in batch])
        for row, backup in zip(batch, backups, strict=True):
            if backup != row["before"]:
                raise RedisError(f"backup verification failed for {row['key']}")

    for client, batch in grouped:
        replies = client.pipeline(
            [("SET", row["key"], row["after"], "XX") for row in batch]
        )
        if any(reply != "OK" for reply in replies):
            raise RedisError(f"Actor rewrite failed through {client.endpoint}")
    for client, batch in grouped:
        rewritten = client.pipeline([("GET", row["key"]) for row in batch])
        for row, actual in zip(batch, rewritten, strict=True):
            if actual != row["after"]:
                raise RedisError(f"Actor verification failed for {row['key']}")


def render_evidence(
    args: argparse.Namespace,
    candidates: Sequence[dict[str, Any]],
    already_current: int,
    statuses: Counter[str],
) -> str:
    evidence = {
        "schema": "substrate-v0.0.9-actor-compatibility/v2",
        "targetVersion": "0.0.9",
        "mode": "apply" if args.apply else "dry-run",
        "endpoints": sorted(args.endpoint),
        "candidateCount": len(candidates),
        "alreadyCurrentCount": already_current,
        "statusCounts": dict(sorted(statuses.items())),
        "records": [
            {
                "key": row["key"],
                "backupKey": row["backupKey"],
                "beforeChecksum": checksum(row["before"]),
                "afterChecksum": checksum(row["after"]),
                "status": row["status"],
            }
            for row in candidates
        ],
    }
    return json.dumps(evidence, indent=2, sort_keys=True) + "\n"


def main() -> int:
    args = parse_args()
    clients: list[RedisConnection] = []
    try:
        for endpoint in args.endpoint:
            clients.append(RedisConnection(endpoint))
        candidates, already_current, statuses = collect_candidates(
            clients,
            args.allow_status,
        )
        if args.apply and len(candidates) != args.expected_candidate_count:
            raise ValueError(
                "reviewed candidate count changed: "
                f"expected {args.expected_candidate_count}, found {len(candidates)}"
            )
        if args.apply:
            apply_candidates(candidates)

        rendered = render_evidence(args, candidates, already_current, statuses)
        if args.evidence:
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0
    finally:
        for client in clients:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
