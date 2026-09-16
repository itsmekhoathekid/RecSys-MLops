from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from jenkins.python.agent_registry_release import (
    build_resource,
    load_catalog,
    registry_tag,
    runtime_records,
    seal_lock,
    validate_lock,
    validate_readback,
    write_chart_record,
)
from jenkins.python.helm_oci import normalize_package
from jenkins.python.release_plan import create_release_plan, load_deploy_config

COMMIT = "a" * 40
REGISTRY = "asia-southeast1-docker.pkg.dev/project/recsys"


def _resource(artifact_id: str) -> dict:
    spec = load_catalog()[artifact_id]
    chart_spec = load_deploy_config()["artifacts"][spec["chartArtifact"]]
    chart = f"oci://{REGISTRY}/{chart_spec['repository']}@sha256:" + "c" * 64
    image = f"{REGISTRY}/{spec.get('image', 'unused')}@sha256:" + "b" * 64
    return build_resource(
        artifact_id,
        commit=COMMIT,
        git_url="https://github.com/example/recsys.git",
        chart_reference=chart,
        image_reference=image if spec["kind"] == "mcp" else "",
        remote_url=(
            "http://recsys-mcp.kagent.svc.cluster.local:8080/mcp"
            if spec["kind"] == "mcp"
            else ""
        ),
    )


def _sealed_lock(tmp_path: Path) -> tuple[dict, dict, Path, Path]:
    plan = create_release_plan(
        [
            "feature_rag_mcp",
            "context_agent",
            "recommendation_mcp",
            "recommendation_agent",
            "coordinator_agent",
        ],
        commit=COMMIT,
    )
    manifest_dir = tmp_path / "manifests"
    readback_dir = tmp_path / "readbacks"
    manifest_dir.mkdir()
    readback_dir.mkdir()
    for artifact_id in load_catalog():
        resource = _resource(artifact_id)
        (manifest_dir / f"{artifact_id}.json").write_text(json.dumps(resource))
        (readback_dir / f"{artifact_id}.json").write_text(json.dumps(resource))
    return plan, seal_lock(plan, manifest_dir, readback_dir), manifest_dir, readback_dir


def test_release_catalog_and_plan_have_explicit_registry_first_phases():
    config = load_deploy_config()
    units = {unit["name"]: unit for unit in config["units"]}
    assert all(unit["phase"] in {"publish", "deploy"} for unit in units.values())
    assert registry_tag(COMMIT) == "0.2.0-gaaaaaaaaaaaa"

    plan = create_release_plan(
        [
            "feature_rag_mcp",
            "context_agent",
            "recommendation_mcp",
            "recommendation_agent",
            "coordinator_agent",
        ],
        commit=COMMIT,
    )
    assert plan["version"] == 3
    assert plan["publishUnits"] == [
        "feature-rag-mcp-registry",
        "context-agent-registry",
        "recommendation-mcp-registry",
        "recommendation-agent-registry",
        "coordinator-agent-registry",
    ]
    assert all(units[name]["phase"] == "publish" for name in plan["publishUnits"])
    assert all(units[name]["phase"] == "deploy" for name in plan["deployUnits"])

    with pytest.raises(ValueError, match="full 40-character"):
        create_release_plan(["context_agent"], commit="")


def test_manifest_is_deterministic_and_contains_immutable_artifacts():
    resource = _resource("feature-rag-mcp")
    assert resource == _resource("feature-rag-mcp")
    annotations = resource["metadata"]["annotations"]
    assert annotations["recsys.dev/runtime-image"].startswith(
        f"{REGISTRY}/recsys-feature-rag-mcp@sha256:"
    )
    assert annotations["recsys.dev/helm-chart"].startswith(
        f"oci://{REGISTRY}/helm/recsys-feature-rag-mcp@sha256:"
    )
    assert annotations["recsys.dev/git-commit"] == COMMIT
    assert annotations["recsys.dev/deployment-driver"] == "helm"
    assert annotations["recsys.dev/contract-sha256"].startswith("sha256:")
    assert resource["spec"]["remote"]["type"] == "streamable-http"


def test_agent_dependencies_use_exact_same_release_tag():
    coordinator = _resource("coordinator-agent")
    annotations = coordinator["metadata"]["annotations"]
    tag = registry_tag(COMMIT)
    assert annotations["recsys.dev/dependencies"].split(",") == [
        f"recsys/recsys-feature-rag-mcp@{tag}",
        f"recsys/recsys-recommendation-mcp@{tag}",
        f"recsys/recsys-context-agent-sandbox@{tag}",
        f"recsys/recsys-recommendation-agent-sandbox@{tag}",
    ]
    assert [item["tag"] for item in coordinator["spec"]["mcpServers"]] == [
        tag,
        tag,
    ]


def test_readback_rejects_tampered_contract():
    expected = _resource("context-agent")
    tampered = copy.deepcopy(expected)
    tampered["spec"]["description"] = "tampered"
    with pytest.raises(ValueError, match="spec does not match"):
        validate_readback(expected, tampered)

    missing_kind = copy.deepcopy(expected)
    del missing_kind["kind"]
    with pytest.raises(ValueError, match="kind does not match"):
        validate_readback(expected, missing_kind)


def test_lock_is_fail_closed_for_stale_commit_and_dependency(tmp_path):
    plan, lock, manifest_dir, readback_dir = _sealed_lock(tmp_path)
    entry = validate_lock(
        plan,
        lock,
        "coordinator-agent",
        manifest_dir=manifest_dir,
        readback_dir=readback_dir,
    )
    assert entry["registryRef"].endswith("@0.2.0-gaaaaaaaaaaaa")

    records = runtime_records(plan, lock, manifest_dir, readback_dir)
    assert [record["artifact"] for record in records] == list(load_catalog())
    assert records[0]["namespace"] == "kagent"
    assert records[0]["remoteUrlKey"] == "featureRag"
    assert records[0]["image"] == lock["artifacts"]["feature-rag-mcp"]["image"]
    assert records[1]["resourceName"] == "recsys-context-agent-sandbox"

    stale = copy.deepcopy(lock)
    stale["gitCommit"] = "d" * 40
    with pytest.raises(ValueError, match="Git commit is stale"):
        validate_lock(plan, stale)

    tampered = copy.deepcopy(lock)
    tampered["artifacts"]["coordinator-agent"]["dependencies"] = []
    with pytest.raises(ValueError, match="dependencies are invalid"):
        validate_lock(plan, tampered)

    checksum_tampered = copy.deepcopy(lock)
    checksum_tampered["artifacts"]["context-agent"]["contractSha256"] = (
        "sha256:" + "e" * 64
    )
    with pytest.raises(ValueError, match="does not match evidence"):
        validate_lock(
            plan,
            checksum_tampered,
            manifest_dir=manifest_dir,
            readback_dir=readback_dir,
        )


def test_chart_record_requires_oci_digest(tmp_path):
    output = tmp_path / "chart.json"
    chart = f"oci://{REGISTRY}/helm/recsys-kagent-agent@sha256:" + "c" * 64
    record = write_chart_record(output, "context-agent-chart", COMMIT, chart)
    assert record["reference"] == chart
    with pytest.raises(ValueError, match="immutable"):
        write_chart_record(output, "context-agent-chart", COMMIT, "oci://chart:latest")


def test_registry_transport_is_append_only():
    source = "\n".join(
        path.read_text()
        for path in sorted(Path("jenkins/scripts/deploy/agentic").glob("registry*.sh"))
    )
    verifier = Path("jenkins/scripts/entrypoints/release_verify.sh").read_text()
    assert "arctl delete" not in source
    assert "publish_context_agent_registry" not in source
    assert "publish_agent_registry_artifact" in source
    assert "verify_agent_registry_runtime_lock" in source
    assert "verify_agent_registry_runtime_lock" in verifier


def test_registry_publication_and_workload_deployment_use_separate_entrypoints():
    publisher = Path(
        "jenkins/scripts/entrypoints/release_publish_unit.sh"
    ).read_text()
    deployer = Path("jenkins/scripts/deploy/release_unit_runtime.sh").read_text()
    pipeline = Path("jenkins/pipeline/component_pipeline.groovy").read_text()

    assert 'publish_agent_registry_artifact "${unit_registry_artifact}"' in publisher
    assert "publish_agent_registry_artifact" not in deployer
    assert pipeline.index("release_publish_unit.sh") < pipeline.index(
        "def deployProductionRelease()"
    )
    deploy_transaction = pipeline.split("def deployProductionRelease()", 1)[1]
    assert "release_publish_unit.sh" not in deploy_transaction


def test_parallel_publish_layers_share_one_registry_transport_lock(tmp_path):
    plan = create_release_plan(
        [
            "feature_rag_mcp",
            "context_agent",
            "recommendation_mcp",
            "recommendation_agent",
            "coordinator_agent",
        ],
        commit=COMMIT,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    output = subprocess.check_output(
        [
            "python3",
            "jenkins/python/release_plan.py",
            "plan-units",
            "--plan",
            str(plan_path),
            "--phase",
            "publish",
        ],
        text=True,
    )
    rows = [line.split("\t") for line in output.splitlines()]
    assert len(rows) == 5
    assert {row[2] for row in rows} == {"agent-registry:catalog"}


def test_helm_package_normalization_is_deterministic(tmp_path):
    package = tmp_path / "chart.tgz"

    def write_package(mtime: int) -> None:
        with tarfile.open(package, mode="w:gz") as archive:
            content = b"apiVersion: v2\nname: chart\nversion: 1.0.0\n"
            member = tarfile.TarInfo("chart/Chart.yaml")
            member.size = len(content)
            member.mtime = mtime
            archive.addfile(member, io.BytesIO(content))
        normalize_package(package)

    write_package(1)
    first = hashlib.sha256(package.read_bytes()).hexdigest()
    write_package(2)
    second = hashlib.sha256(package.read_bytes()).hexdigest()

    assert first == second


@pytest.mark.parametrize(
    ("chart", "kind", "name"),
    [
        ("recsys-feature-rag-mcp", "Deployment", "recsys-feature-rag-mcp"),
        ("recsys-kagent-agent", "SandboxAgent", "recsys-context-agent-sandbox"),
        (
            "recsys-recommendation-mcp",
            "Deployment",
            "recsys-recommendation-mcp",
        ),
        (
            "recsys-recommendation-agent",
            "SandboxAgent",
            "recsys-recommendation-agent-sandbox",
        ),
        (
            "recsys-coordinator-agent",
            "SandboxAgent",
            "recsys-coordinator-agent-sandbox",
        ),
    ],
)
def test_agentic_resources_carry_registry_lock_annotations(chart, kind, name):
    registry_ref = "recsys/test@0.2.0-gaaaaaaaaaaaa"
    contract = "sha256:" + "f" * 64
    output = subprocess.check_output(
        [
            "helm",
            "template",
            "registry-gate",
            f"infra/helm/{chart}",
            "--set-string",
            f"releaseMetadata.registryRef={registry_ref}",
            "--set-string",
            "releaseMetadata.version=0.2.0-gaaaaaaaaaaaa",
            "--set-string",
            f"releaseMetadata.contractSha256={contract}",
        ],
        text=True,
    )
    resource = next(
        item
        for item in yaml.safe_load_all(output)
        if item
        and item.get("kind") == kind
        and item.get("metadata", {}).get("name") == name
    )
    expected = {
        "recsys.dev/agent-registry-ref": registry_ref,
        "recsys.dev/agent-release-version": "0.2.0-gaaaaaaaaaaaa",
        "recsys.dev/contract-sha256": contract,
    }
    assert expected.items() <= resource["metadata"]["annotations"].items()
    if kind == "Deployment":
        assert (
            expected.items()
            <= resource["spec"]["template"]["metadata"]["annotations"].items()
        )
