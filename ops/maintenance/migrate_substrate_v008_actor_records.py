#!/usr/bin/env python3
"""Back up and migrate inactive Substrate v0.0.8 Actor records in Valkey.

The tool talks RESP directly to explicitly supplied Valkey primary endpoints.
It never discovers or mutates Kubernetes resources. Dry-run is the default.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from jenkins.python.substrate_state import (  # noqa: E402
    LEGACY_ACTOR_PREFIX,
    canonical_json,
    checksum,
    colocated_backup_key,
    migrate_v008_actor,
)


class RedisError(RuntimeError):
    pass


class RedisConnection:
    def __init__(self, endpoint: str, timeout: float = 10.0):
        host, separator, port = endpoint.rpartition(":")
        if not separator or not host or not port.isdigit():
            raise ValueError(f"endpoint must be HOST:PORT: {endpoint}")
        self.endpoint = endpoint
        self._socket = socket.create_connection((host, int(port)), timeout=timeout)
        self._stream = self._socket.makefile("rb")

    def close(self) -> None:
        self._stream.close()
        self._socket.close()

    def command(self, *parts: str) -> Any:
        encoded = [part.encode("utf-8") for part in parts]
        request = [f"*{len(encoded)}\r\n".encode()]
        for part in encoded:
            request.extend((f"${len(part)}\r\n".encode(), part, b"\r\n"))
        self._socket.sendall(b"".join(request))
        return self._read_response()

    def _read_line(self) -> bytes:
        line = self._stream.readline()
        if not line.endswith(b"\r\n"):
            raise RedisError(f"truncated RESP reply from {self.endpoint}")
        return line[:-2]

    def _read_response(self) -> Any:
        prefix = self._stream.read(1)
        if prefix == b"+":
            return self._read_line().decode("utf-8")
        if prefix == b"-":
            raise RedisError(self._read_line().decode("utf-8"))
        if prefix == b":":
            return int(self._read_line())
        if prefix == b"$":
            length = int(self._read_line())
            if length == -1:
                return None
            payload = self._stream.read(length)
            if self._stream.read(2) != b"\r\n":
                raise RedisError(f"invalid bulk RESP reply from {self.endpoint}")
            return payload.decode("utf-8")
        if prefix == b"*":
            length = int(self._read_line())
            if length == -1:
                return None
            return [self._read_response() for _ in range(length)]
        raise RedisError(f"unsupported RESP prefix from {self.endpoint}: {prefix!r}")


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
        if args.confirm != "rewrite-reviewed-inactive-actors":
            parser.error("--apply requires --confirm rewrite-reviewed-inactive-actors")
    return args


def scan_legacy_records(client: RedisConnection) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    cursor = "0"
    while True:
        reply = client.command(
            "SCAN", cursor, "MATCH", f"{LEGACY_ACTOR_PREFIX}*", "COUNT", "500"
        )
        cursor, keys = str(reply[0]), reply[1]
        for key in keys:
            raw = client.command("GET", key)
            if raw is not None:
                records.append((key, raw))
        if cursor == "0":
            return sorted(records)


def main() -> int:
    args = parse_args()

    clients = [RedisConnection(endpoint) for endpoint in args.endpoint]
    try:
        candidates: list[dict[str, Any]] = []
        already_current = 0
        statuses: Counter[str] = Counter()
        for client in clients:
            for key, raw in scan_legacy_records(client):
                migrated = migrate_v008_actor(
                    key,
                    raw,
                    additionally_allowed_statuses=args.allow_status,
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

        if args.apply and len(candidates) != args.expected_candidate_count:
            raise ValueError(
                "reviewed candidate count changed: "
                f"expected {args.expected_candidate_count}, found {len(candidates)}"
            )

        if args.apply:
            # Complete and verify every backup before rewriting any actor key.
            for record in candidates:
                client = record["client"]
                created = client.command(
                    "SET", record["backupKey"], record["before"], "NX"
                )
                if created not in {"OK", None}:
                    raise RedisError(f"unexpected backup reply for {record['key']}")
                if client.command("GET", record["backupKey"]) != record["before"]:
                    raise RedisError(f"backup verification failed for {record['key']}")
            for record in candidates:
                client = record["client"]
                if client.command("SET", record["key"], record["after"], "XX") != "OK":
                    raise RedisError(f"actor rewrite failed for {record['key']}")
                if client.command("GET", record["key"]) != record["after"]:
                    raise RedisError(f"actor verification failed for {record['key']}")

        evidence = {
            "schema": "substrate-v0.0.8-to-v0.0.9-actor-metadata/v1",
            "mode": "apply" if args.apply else "dry-run",
            "endpoints": sorted(args.endpoint),
            "candidateCount": len(candidates),
            "alreadyCurrentCount": already_current,
            "statusCounts": dict(sorted(statuses.items())),
            "records": [
                {
                    "key": record["key"],
                    "backupKey": record["backupKey"],
                    "beforeChecksum": checksum(record["before"]),
                    "afterChecksum": checksum(record["after"]),
                    "status": record["status"],
                }
                for record in candidates
            ],
        }
        rendered = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
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
