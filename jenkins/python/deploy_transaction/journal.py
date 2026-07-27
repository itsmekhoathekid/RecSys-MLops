from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"COMMITTED", "ROLLED_BACK"}
VALID_STATES = {
    "PREFLIGHT",
    "SNAPSHOT",
    "APPLYING",
    "VERIFYING",
    "COMMITTED",
    "ROLLING_BACK",
    "ROLLED_BACK",
    "ROLLBACK_FAILED",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize(path: Path, transaction_id: str, component: str, git_sha: str) -> None:
    save(
        path,
        {
            "schemaVersion": 1,
            "transactionId": transaction_id,
            "component": component,
            "gitSha": git_sha,
            "state": "PREFLIGHT",
            "startedAt": utc_now(),
            "updatedAt": utc_now(),
            "helmReleases": [],
            "externalState": [],
            "healthTests": [],
            "events": [{"state": "PREFLIGHT", "at": utc_now()}],
        },
    )


def transition(path: Path, state: str, message: str = "") -> None:
    if state not in VALID_STATES:
        raise ValueError(f"invalid transaction state: {state}")
    payload = load(path)
    payload["state"] = state
    payload["updatedAt"] = utc_now()
    event = {"state": state, "at": utc_now()}
    if message:
        event["message"] = message
    payload.setdefault("events", []).append(event)
    save(path, payload)


def add_release(
    path: Path,
    release: str,
    namespace: str,
    existed: bool,
    revision: str,
    workload_snapshot_path: str = "",
) -> None:
    payload = load(path)
    releases = payload.setdefault("helmReleases", [])
    if any(
        item["release"] == release and item["namespace"] == namespace
        for item in releases
    ):
        return
    releases.append(
        {
            "release": release,
            "namespace": namespace,
            "existed": existed,
            "revision": revision,
            "workloadSnapshotPath": workload_snapshot_path,
            "rollback": "pending",
        }
    )
    payload["updatedAt"] = utc_now()
    save(path, payload)


def add_external(path: Path, kind: str, state_path: str) -> None:
    payload = load(path)
    states = payload.setdefault("externalState", [])
    record = {"kind": kind, "statePath": state_path, "rollback": "pending"}
    if record not in states:
        states.append(record)
    payload["updatedAt"] = utc_now()
    save(path, payload)


def add_health_test(path: Path, profile: str, status: str, report_path: str) -> None:
    payload = load(path)
    payload.setdefault("healthTests", []).append(
        {
            "profile": profile,
            "status": status,
            "reportPath": report_path,
            "at": utc_now(),
        }
    )
    payload["updatedAt"] = utc_now()
    save(path, payload)


def mark_rollback(
    path: Path, collection: str, identifier: str, status: str, detail: str = ""
) -> None:
    payload = load(path)
    key = "helmReleases" if collection == "helm" else "externalState"
    identity_key = "release" if collection == "helm" else "statePath"
    for item in payload.get(key, []):
        if item.get(identity_key) == identifier:
            item["rollback"] = status
            if detail:
                item["rollbackDetail"] = detail
            break
    else:
        raise KeyError(f"{collection} rollback record not found: {identifier}")
    payload["updatedAt"] = utc_now()
    save(path, payload)


def iter_records(path: Path, collection: str) -> list[dict[str, Any]]:
    payload = load(path)
    key = "helmReleases" if collection == "helm" else "externalState"
    return list(reversed(payload.get(key, [])))


def find_blocking(root: Path, component: str, exclude: Path | None = None) -> list[Path]:
    if not root.exists():
        return []
    blocking: list[Path] = []
    for path in root.glob("*/transaction.json"):
        if exclude is not None and path.resolve() == exclude.resolve():
            continue
        try:
            payload = load(path)
        except (OSError, ValueError, json.JSONDecodeError):
            blocking.append(path)
            continue
        if payload.get("component") == component and payload.get("state") not in TERMINAL_STATES:
            blocking.append(path)
    return blocking


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage durable component deployment journals.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--path", type=Path, required=True)
    init_parser.add_argument("--transaction-id", required=True)
    init_parser.add_argument("--component", required=True)
    init_parser.add_argument("--git-sha", required=True)

    state_parser = subparsers.add_parser("state")
    state_parser.add_argument("--path", type=Path, required=True)
    state_parser.add_argument("--state", choices=sorted(VALID_STATES), required=True)
    state_parser.add_argument("--message", default="")

    release_parser = subparsers.add_parser("add-release")
    release_parser.add_argument("--path", type=Path, required=True)
    release_parser.add_argument("--release", required=True)
    release_parser.add_argument("--namespace", required=True)
    release_parser.add_argument("--existed", choices=["0", "1"], required=True)
    release_parser.add_argument("--revision", default="")
    release_parser.add_argument("--workload-snapshot-path", default="")

    external_parser = subparsers.add_parser("add-external")
    external_parser.add_argument("--path", type=Path, required=True)
    external_parser.add_argument("--kind", required=True)
    external_parser.add_argument("--state-path", required=True)

    test_parser = subparsers.add_parser("add-test")
    test_parser.add_argument("--path", type=Path, required=True)
    test_parser.add_argument("--profile", required=True)
    test_parser.add_argument("--status", choices=["passed", "failed"], required=True)
    test_parser.add_argument("--report-path", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--path", type=Path, required=True)
    list_parser.add_argument("--collection", choices=["helm", "external"], required=True)

    mark_parser = subparsers.add_parser("mark-rollback")
    mark_parser.add_argument("--path", type=Path, required=True)
    mark_parser.add_argument("--collection", choices=["helm", "external"], required=True)
    mark_parser.add_argument("--identifier", required=True)
    mark_parser.add_argument("--status", required=True)
    mark_parser.add_argument("--detail", default="")

    blocking_parser = subparsers.add_parser("blocking")
    blocking_parser.add_argument("--root", type=Path, required=True)
    blocking_parser.add_argument("--component", required=True)
    blocking_parser.add_argument("--exclude", type=Path)

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("--path", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "init":
        initialize(args.path, args.transaction_id, args.component, args.git_sha)
    elif args.command == "state":
        transition(args.path, args.state, args.message)
    elif args.command == "add-release":
        add_release(
            args.path,
            args.release,
            args.namespace,
            args.existed == "1",
            args.revision,
            args.workload_snapshot_path,
        )
    elif args.command == "add-external":
        add_external(args.path, args.kind, args.state_path)
    elif args.command == "add-test":
        add_health_test(args.path, args.profile, args.status, args.report_path)
    elif args.command == "list":
        for record in iter_records(args.path, args.collection):
            print(json.dumps(record, sort_keys=True))
    elif args.command == "mark-rollback":
        mark_rollback(
            args.path,
            args.collection,
            args.identifier,
            args.status,
            args.detail,
        )
    elif args.command == "blocking":
        blocking = find_blocking(args.root, args.component, args.exclude)
        for path in blocking:
            print(path)
        return 1 if blocking else 0
    elif args.command == "show":
        print(json.dumps(load(args.path), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
