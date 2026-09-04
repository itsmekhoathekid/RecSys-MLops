from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jenkins.python.release_plan import load_deploy_config, load_release_plan


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _json_command(*args: str) -> Any:
    result = _run(*args, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _helm_snapshot(unit: dict[str, Any]) -> dict[str, Any]:
    history = _json_command(
        "helm", "history", unit["release"], "-n", unit["namespace"], "-o", "json"
    )
    deployed = [item for item in history or [] if item.get("status") == "deployed"]
    revision = max((int(item["revision"]) for item in deployed), default=0)
    return {"revision": revision}


def _namespace_workloads(namespace: str) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    for kind in (
        "deployments",
        "statefulsets",
        "sandboxagents.kagent.dev",
        "workerpools.kagent.dev",
        "remotemcpservers.kagent.dev",
    ):
        payload = _json_command("kubectl", "-n", namespace, "get", kind, "-o", "json")
        if payload is not None:
            summaries = []
            for item in payload.get("items", []):
                metadata = item.get("metadata", {})
                pod_spec = (
                    item.get("spec", {}).get("template", {}).get("spec", {})
                    if kind in {"deployments", "statefulsets"}
                    else {}
                )
                summaries.append(
                    {
                        "name": metadata.get("name"),
                        "generation": metadata.get("generation"),
                        "resourceVersion": metadata.get("resourceVersion"),
                        "modelConfigRevision": metadata.get("annotations", {}).get(
                            "recsys.ai/model-config-revision"
                        ),
                        "images": [
                            container.get("image")
                            for container in pod_spec.get("containers", [])
                            if container.get("image")
                        ],
                    }
                )
            resources[kind] = summaries
    return resources


def snapshot(plan_path: Path, evidence_path: Path) -> dict[str, Any]:
    plan = load_release_plan(plan_path)
    units_by_name = {
        unit["name"]: unit for unit in load_deploy_config()["units"]
    }
    units = [units_by_name[name] for name in plan["deployUnits"]]
    namespaces = sorted({unit["namespace"] for unit in units if unit["namespace"]})
    payload = {
        "version": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "commit": plan["commit"],
        "units": [
            {
                "name": unit["name"],
                "kind": unit["kind"],
                "namespace": unit["namespace"],
                "release": unit["release"],
                "helm": _helm_snapshot(unit) if unit["kind"] == "helm" else None,
            }
            for unit in units
        ],
        "workloads": {
            namespace: _namespace_workloads(namespace) for namespace in namespaces
        },
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def _current_revision(unit: dict[str, Any]) -> int:
    return int(_helm_snapshot(unit)["revision"])


def rollback(snapshot_path: Path, evidence_path: Path) -> dict[str, Any]:
    before = json.loads(snapshot_path.read_text(encoding="utf-8"))
    actions: list[dict[str, Any]] = []
    for unit in reversed(before["units"]):
        if unit["kind"] != "helm":
            continue
        previous = int((unit.get("helm") or {}).get("revision", 0))
        current = _current_revision(unit)
        action = {
            "name": unit["name"],
            "namespace": unit["namespace"],
            "release": unit["release"],
            "previousRevision": previous,
            "currentRevision": current,
            "operation": "unchanged",
            "status": "skipped",
        }
        if current != previous:
            if previous > 0:
                action["operation"] = "rollback"
                command = (
                    "helm",
                    "rollback",
                    unit["release"],
                    str(previous),
                    "-n",
                    unit["namespace"],
                    "--wait",
                    "--cleanup-on-fail",
                    "--timeout",
                    "10m",
                )
            else:
                action["operation"] = "uninstall"
                command = (
                    "helm",
                    "uninstall",
                    unit["release"],
                    "-n",
                    unit["namespace"],
                    "--wait",
                    "--timeout",
                    "10m",
                )
            result = _run(*command, check=False)
            action["status"] = "success" if result.returncode == 0 else "failed"
            action["stdout"] = result.stdout[-4000:]
            action["stderr"] = result.stderr[-4000:]
        actions.append(action)
    payload = {
        "version": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "actions": actions,
        "success": all(action["status"] != "failed" for action in actions),
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if not payload["success"]:
        raise RuntimeError("one or more Helm rollback actions failed")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot and rollback a release plan.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--plan", required=True, type=Path)
    snapshot_parser.add_argument("--output", required=True, type=Path)
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--snapshot", required=True, type=Path)
    rollback_parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        snapshot(args.plan, args.output)
    else:
        rollback(args.snapshot, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
