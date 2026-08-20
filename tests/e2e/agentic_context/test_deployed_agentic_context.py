from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(
    os.getenv("RUN_AGENTIC_E2E") != "1",
    reason="requires the production kagent, Substrate, APIs, and Registry",
)
def test_regular_and_sandbox_agents_return_grounded_context():
    assert os.getenv("AGENTIC_SMOKE_CHUNK_ID"), "exact chunk fixture is required"
    subprocess.run(
        ["bash", "ops/validation/agentic_context_smoke.sh"],
        cwd=ROOT,
        check=True,
        env=os.environ.copy(),
    )
