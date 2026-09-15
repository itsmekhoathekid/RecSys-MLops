"""Check config ownership, snapshot invalidation and release ordering."""
import subprocess
from pathlib import Path

import yaml

from jenkins.python.release_plan import create_release_plan
from jenkins.python.change_detection.detector import ChangedFile, detect_changed_components

ROOT = Path(__file__).resolve().parents[3]


def render(chart, *args):
    result = subprocess.run(
        ["helm", "template", "test", str(ROOT / "infra/helm" / chart),
         "-n", "kagent", *args], check=True, capture_output=True, text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def test_global_config_and_agents_use_native_modelconfig_reference():
    configs = render("recsys-global-model-config")
    assert len(configs) == 1
    assert configs[0]["kind"] == "ModelConfig"
    name = configs[0]["metadata"]["name"]
    assert name != "default-model-config"  # Do not adopt platform-owned config.
    for chart in ("recsys-kagent-agent", "recsys-recommendation-agent", "recsys-coordinator-agent"):
        docs = render(chart)
        agent = next(doc for doc in docs if doc["kind"] == "SandboxAgent")
        assert agent["spec"]["declarative"]["modelConfig"] == name
        assert "recsys.ai/model-config-revision" not in agent["metadata"].get("annotations", {})
        assert "Runtime model configuration revision:" not in agent["spec"]["declarative"]["systemMessage"]


def test_global_config_deploys_before_each_consumer():
    plan = create_release_plan(["context_agent", "recommendation_agent", "coordinator_agent"])
    units = plan["deployUnits"]
    for name in ("context-agent", "recommendation-agent", "coordinator-agent"):
        assert units.index("global-model-config") < units.index(name)


def test_global_values_change_selects_all_snapshot_consumers():
    result = detect_changed_components([
        ChangedFile("M", "infra/helm/recsys-global-model-config/values.yaml")
    ])
    assert {"context_agent", "recommendation_agent", "coordinator_agent"} <= set(result.component_names)
    units = result.release_plan["deployUnits"]
    for name in ("context-agent", "recommendation-agent", "coordinator-agent"):
        assert units.index("global-model-config") < units.index(name)
