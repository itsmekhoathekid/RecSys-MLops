from __future__ import annotations

import json
from pathlib import Path

from jenkins.python import deployment_transaction


def test_rollback_runs_changed_helm_releases_in_reverse_order(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    evidence_path = tmp_path / "rollback.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "units": [
                    {
                        "name": "dependency",
                        "kind": "helm",
                        "namespace": "ns",
                        "release": "dependency",
                        "helm": {"revision": 2},
                    },
                    {
                        "name": "consumer",
                        "kind": "helm",
                        "namespace": "ns",
                        "release": "consumer",
                        "helm": {"revision": 4},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(deployment_transaction, "_current_revision", lambda unit: 9)
    commands: list[tuple[str, ...]] = []

    def fake_run(*args: str, check: bool = True):
        commands.append(args)
        return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(deployment_transaction, "_run", fake_run)
    evidence = deployment_transaction.rollback(snapshot_path, evidence_path)

    assert [command[2] for command in commands] == ["consumer", "dependency"]
    assert [action["previousRevision"] for action in evidence["actions"]] == [4, 2]
    assert evidence["success"] is True


def test_rollback_uninstalls_a_release_created_by_the_failed_build(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "units": [
                    {
                        "name": "new-release",
                        "kind": "helm",
                        "namespace": "ns",
                        "release": "new-release",
                        "helm": {"revision": 0},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(deployment_transaction, "_current_revision", lambda unit: 1)
    commands: list[tuple[str, ...]] = []

    def fake_run(*args: str, check: bool = True):
        commands.append(args)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(deployment_transaction, "_run", fake_run)
    deployment_transaction.rollback(snapshot_path, tmp_path / "evidence.json")
    assert commands[0][:3] == ("helm", "uninstall", "new-release")
