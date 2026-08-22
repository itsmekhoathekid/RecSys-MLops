from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_recommendation_release_has_no_context_or_rag_reference() -> None:
    chart = ROOT / "infra/helm/recsys-recommendation-agent"
    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in chart.rglob("*")
        if path.is_file()
    )
    assert "recsys-context-agent" not in content
    assert "recsys-feature-rag-mcp" not in content
    values = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    assert values["mcp"]["tools"] == ["get_personalized_recommendations"]
    assert values["sandbox"]["allowedDomains"] == [
        "recsys-recommendation-mcp.kagent.svc.cluster.local"
    ]
