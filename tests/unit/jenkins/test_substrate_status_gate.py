from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[3]
GATE = ROOT / "ops/validation/substrate_status_gate.sh"


def _run_gate(tmp_path: Path, payload: dict) -> subprocess.CompletedProcess[str]:
    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    mock_curl = mock_bin / "curl"
    mock_curl.write_text(
        """#!/usr/bin/env python3
import os
import pathlib
import sys

output = pathlib.Path(sys.argv[sys.argv.index("-o") + 1])
output.write_text(os.environ["MOCK_STATUS_PAYLOAD"], encoding="utf-8")
""",
        encoding="utf-8",
    )
    mock_curl.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}:{env['PATH']}"
    env["MOCK_STATUS_PAYLOAD"] = json.dumps(payload)
    return subprocess.run(
        [
            "bash",
            str(GATE),
            "--status-url",
            "http://mock.invalid/api/substrate/status",
            "--output",
            str(tmp_path / "status.json"),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_substrate_status_gate_archives_complete_inventory(tmp_path: Path):
    payload = {
        "error": False,
        "data": {"enabled": True, "ateApiError": "", "actors": []},
    }
    completed = _run_gate(tmp_path, payload)
    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / "status.json").read_text()) == payload


def test_substrate_status_gate_rejects_ate_api_decode_failure(tmp_path: Path):
    completed = _run_gate(
        tmp_path,
        {
            "error": False,
            "data": {
                "enabled": True,
                "ateApiError": 'unknown field "actorId"',
                "actors": None,
            },
        },
    )
    assert completed.returncode != 0
    assert "ATE API is unhealthy" in completed.stderr
    assert not (tmp_path / "status.json").exists()
