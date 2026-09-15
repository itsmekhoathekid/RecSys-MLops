from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GATE = ROOT / "ops/validation/mcp_auth_rotation_gate.sh"


def _executable(path: Path, source: str) -> Path:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)
    return path


def _mock_runtime(
    tmp_path: Path,
    *,
    include_expected_actor: bool,
    status_complete: bool = True,
    exact_actor_template: str = "recommendation-template-v2",
    exact_actor_is_stale: bool = False,
) -> tuple[dict[str, str], Path]:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    state_path = tmp_path / "actor.json"

    _executable(
        mock_bin / "kubectl",
        r'''
        #!/usr/bin/env python3
        import json
        import os
        import sys
        from pathlib import Path

        args = sys.argv[1:]
        joined = " ".join(args)
        revision = "v2"
        if "sandboxagents.kagent.dev/recsys-recommendation-agent-sandbox" in joined:
            if "jsonpath" in joined:
                print("7", end="")
            else:
                print(json.dumps({
                    "metadata": {
                        "name": "recsys-recommendation-agent-sandbox",
                        "namespace": "kagent",
                        "uid": "sandbox-uid",
                        "generation": 7,
                        "annotations": {"recsys.ai/mcp-auth-revision": revision},
                    },
                    "spec": {"declarative": {"deployment": {"env": [{
                        "name": "RECSYS_MCP_AUTH_REVISION", "value": revision,
                    }]}}},
                    "status": {
                        "observedGeneration": 7,
                        "conditions": [{"type": "Accepted", "status": "True"}],
                    },
                }))
        elif "remotemcpservers.kagent.dev/recsys-recommendation-mcp" in joined:
            print(json.dumps({
                "metadata": {
                    "name": "recsys-recommendation-mcp",
                    "uid": "rms-uid",
                    "generation": 3,
                    "annotations": {"recsys.ai/mcp-auth-revision": revision},
                },
                "spec": {
                    "url": "http://recsys-recommendation-mcp-v2.kagent.svc.cluster.local:8080/mcp",
                    "headersFrom": [{
                        "name": "Authorization",
                        "valueFrom": {"name": "recsys-recommendation-mcp-auth-v2"},
                    }],
                },
                "status": {
                    "observedGeneration": 3,
                    "conditions": [{"type": "Accepted", "status": "True"}],
                },
            }))
        elif "get actortemplates.ate.dev " in joined:
            print(json.dumps({"items": [{
                "metadata": {
                    "name": "recommendation-template-v2",
                    "uid": "template-uid",
                    "creationTimestamp": "2026-09-12T00:00:00Z",
                    "annotations": {
                        "kagent.dev/desired-generation": "7",
                        "kagent.dev/actor-template-hash": "shape-v2",
                    },
                },
                "status": {"phase": "Ready", "goldenActorID": "golden-v2"},
            }] }))
        elif "actortemplates.ate.dev/old-template" in joined:
            print(json.dumps({
                "metadata": {
                    "name": "old-template",
                    "labels": {
                        "kagent.dev/sandbox-agent": (
                            "recsys-recommendation-agent-sandbox"
                        ),
                    },
                    "annotations": {"kagent.dev/desired-generation": "6"},
                },
            }))
        elif "secret/recsys-recommendation-mcp-auth-v2" in joined:
            print("secret-uid\t123")
        elif "deployment/recsys-recommendation-mcp-v2" in joined:
            print(json.dumps({"metadata": {
                "name": "recsys-recommendation-mcp-v2",
                "uid": "deployment-uid",
                "generation": 5,
            }}))
        elif "exec valkey-cluster-0" in joined and "valkey-cli" in joined:
            state = Path(os.environ["MOCK_ACTOR_STATE"])
            if state.exists():
                actor = json.loads(state.read_text(encoding="utf-8"))
                print(json.dumps({
                    "metadata": {
                        "atespace": "kagent",
                        "name": actor["actorId"],
                        "createTime": actor["createTime"],
                    },
                    "actorTemplateNamespace": "kagent",
                    "actorTemplateName": os.environ["MOCK_EXACT_ACTOR_TEMPLATE"],
                }))
        else:
            raise SystemExit("unexpected kubectl invocation: " + joined)
        ''',
    )

    _executable(
        mock_bin / "curl",
        r'''
        #!/usr/bin/env python3
        import json
        import os
        from pathlib import Path

        if os.environ["MOCK_STATUS_COMPLETE"] != "true":
            print(json.dumps({
                "error": False,
                "data": {
                    "enabled": True,
                    "ateApiError": "legacy actor record cannot be decoded",
                    "actors": None,
                },
            }))
            raise SystemExit(0)

        actors = [{
            "actorId": "asr-unrelated-concurrent-actor",
            "atespace": "kagent",
            "actorTemplateNamespace": "kagent",
            "actorTemplateName": "recommendation-template-v2",
        }]
        state = Path(os.environ["MOCK_ACTOR_STATE"])
        if state.exists():
            actor_id = json.loads(state.read_text(encoding="utf-8"))["actorId"]
            actors.append({
                "actorId": actor_id,
                "atespace": "kagent",
                "actorTemplateNamespace": "kagent",
                "actorTemplateName": "recommendation-template-v2",
            })
        print(json.dumps({
            "error": False,
            "data": {"enabled": True, "ateApiError": "", "actors": actors},
        }))
        ''',
    )

    smoke = _executable(
        tmp_path / "smoke",
        r'''
        #!/usr/bin/env python3
        from datetime import datetime, timezone
        import hashlib
        import json
        import os
        from pathlib import Path

        context_id = os.environ["RECSYS_FRESH_SESSION_ID"] + "-exact"
        evidence = Path(os.environ["MCP_AUTH_ROTATION_SESSION_EVIDENCE"])
        evidence.write_text(
            json.dumps({"contextIds": [context_id]}, separators=(",", ":")),
            encoding="utf-8",
        )
        if os.environ["MOCK_INCLUDE_EXPECTED_ACTOR"] == "true":
            digest = hashlib.sha256(
                ("kagent/recsys-recommendation-agent-sandbox/" + context_id).encode()
            ).hexdigest()[:24]
            Path(os.environ["MOCK_ACTOR_STATE"]).write_text(
                json.dumps({
                    "actorId": "asr-" + digest,
                    "createTime": (
                        "2000-01-01T00:00:00Z"
                        if os.environ["MOCK_EXACT_ACTOR_IS_STALE"] == "true"
                        else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    ),
                }),
                encoding="utf-8",
            )
        ''',
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{mock_bin}:{env['PATH']}",
            "MOCK_ACTOR_STATE": str(state_path),
            "MOCK_INCLUDE_EXPECTED_ACTOR": str(include_expected_actor).lower(),
            "MOCK_STATUS_COMPLETE": str(status_complete).lower(),
            "MOCK_EXACT_ACTOR_TEMPLATE": exact_actor_template,
            "MOCK_EXACT_ACTOR_IS_STALE": str(exact_actor_is_stale).lower(),
        }
    )
    return env, smoke


def _gate_command(smoke: Path, timeout: int = 3) -> list[str]:
    return [
        "bash",
        str(GATE),
        "--remote-mcp-server",
        "recsys-recommendation-mcp",
        "--sandbox-agent",
        "recsys-recommendation-agent-sandbox",
        "--expected-revision",
        "v2",
        "--expected-url",
        "http://recsys-recommendation-mcp-v2.kagent.svc.cluster.local:8080/mcp",
        "--expected-secret",
        "recsys-recommendation-mcp-auth-v2",
        "--timeout-seconds",
        str(timeout),
        "--poll-seconds",
        "1",
        "--substrate-status-url",
        "http://mock.invalid/api/substrate/status",
        "--",
        str(smoke),
    ]


def test_fresh_smoke_attests_the_exact_context_derived_actor(tmp_path: Path) -> None:
    env, smoke = _mock_runtime(tmp_path, include_expected_actor=True)
    completed = subprocess.run(
        _gate_command(smoke),
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(completed.stdout)
    expected_actor = json.loads(
        (tmp_path / "actor.json").read_text(encoding="utf-8")
    )["actorId"]
    assert evidence["smoke"] == {
        "executed": True,
        "actorTemplateAttested": True,
        "attestationSource": "controller-inventory",
        "newActorCount": 1,
        "actorIds": [expected_actor],
        "freshSessionId": evidence["smoke"]["freshSessionId"],
    }


def test_unrelated_concurrent_actor_cannot_satisfy_fresh_smoke(tmp_path: Path) -> None:
    env, smoke = _mock_runtime(tmp_path, include_expected_actor=False)
    completed = subprocess.run(
        _gate_command(smoke, timeout=1),
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "Fresh smoke actor attestation failed" in completed.stderr


def test_fresh_smoke_falls_back_to_exact_valkey_key_when_inventory_is_broken(
    tmp_path: Path,
) -> None:
    env, smoke = _mock_runtime(
        tmp_path,
        include_expected_actor=True,
        status_complete=False,
    )
    completed = subprocess.run(
        _gate_command(smoke),
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    evidence = json.loads(completed.stdout)
    assert evidence["smoke"]["actorTemplateAttested"] is True
    assert evidence["smoke"]["attestationSource"] == "exact-valkey-key"
    assert evidence["smoke"]["newActorCount"] == 1
    assert "Global actor inventory is incomplete" in completed.stderr


def test_exact_key_fallback_rejects_wrong_actor_template(tmp_path: Path) -> None:
    env, smoke = _mock_runtime(
        tmp_path,
        include_expected_actor=True,
        status_complete=False,
        exact_actor_template="old-template",
    )
    completed = subprocess.run(
        _gate_command(smoke, timeout=1),
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "Fresh smoke actor attestation failed" in completed.stderr


def test_exact_key_fallback_rejects_preexisting_actor(tmp_path: Path) -> None:
    env, smoke = _mock_runtime(
        tmp_path,
        include_expected_actor=True,
        status_complete=False,
        exact_actor_is_stale=True,
    )
    completed = subprocess.run(
        _gate_command(smoke, timeout=1),
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "Fresh smoke actor attestation failed" in completed.stderr


def test_retirement_stays_fail_closed_when_global_inventory_is_broken(
    tmp_path: Path,
) -> None:
    env, smoke = _mock_runtime(
        tmp_path,
        include_expected_actor=True,
        status_complete=False,
    )
    command = _gate_command(smoke)
    separator = command.index("--")
    command[separator:separator] = ["--retire-template", "old-template"]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert "did not return a complete actor inventory" in completed.stderr


def test_rotation_gate_rejects_xtrace_without_echoing_authorization() -> None:
    sentinel = "Bearer must-not-appear-in-xtrace"
    env = os.environ.copy()
    env["MCP_AUTH_ROTATION_SUBSTRATE_AUTHORIZATION"] = sentinel
    completed = subprocess.run(
        ["bash", "-x", str(GATE), "--help"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2
    assert sentinel not in completed.stdout
    assert sentinel not in completed.stderr
