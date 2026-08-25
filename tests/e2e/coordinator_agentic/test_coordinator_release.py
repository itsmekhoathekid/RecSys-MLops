from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_coordinator_release_reuses_canonical_mcp_resources() -> None:
    chart = ROOT / "infra/helm/recsys-coordinator-agent"
    values = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    assert values["mcpServers"]["context"]["name"] == "recsys-feature-rag-mcp"
    assert values["mcpServers"]["recommendation"]["name"] == (
        "recsys-recommendation-mcp"
    )
    assert not (chart / "templates/remotemcpserver.yaml").exists()


@pytest.mark.skipif(
    os.getenv("RUN_COORDINATOR_E2E") != "1",
    reason="requires the deployed coordinator, both specialists, MCPs, and Registry",
)
def test_deployed_coordinator_routes_a2a_and_direct_mcp_requests() -> None:
    subprocess.run(
        ["bash", "ops/validation/coordinator_agentic_smoke.sh"],
        cwd=ROOT,
        check=True,
        env=os.environ.copy(),
    )
