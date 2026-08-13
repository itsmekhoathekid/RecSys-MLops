from __future__ import annotations

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "docs" / "submission"
THIS_FILE = Path(__file__).resolve()


def _runtime_files() -> list[Path]:
    ignored_roots = {
        ".git",
        ".venv",
        ".uv-cache",
        ".docker-metrics",
        ".hypothesis",
        "node_modules",
        "dist",
        "coverage",
        "reports",
        "outputs",
        "artifacts",
        "graphify-out",
        "__pycache__",
    }
    paths: list[Path] = []
    for directory, child_directories, filenames in os.walk(ROOT):
        current = Path(directory)
        child_directories[:] = [
            name
            for name in child_directories
            if name not in ignored_roots and current / name != ARCHIVE
        ]
        for filename in filenames:
            path = current / filename
            if path != THIS_FILE:
                paths.append(path)
    return paths


def test_only_catalog_dockerfiles_exist() -> None:
    catalog = json.loads((ROOT / "images/catalog.json").read_text(encoding="utf-8"))
    expected = {ROOT / spec["dockerfile"] for spec in catalog["images"].values()}
    actual = {
        path
        for path in _runtime_files()
        if path.name == "Dockerfile" or path.name.startswith("Dockerfile.")
    }
    assert actual == expected


def test_retired_runtime_roots_are_absent() -> None:
    retired = (
        "infra/docker",
        "infra/k8s",
        "infra/kubeflow",
        "infra/cloudbuild",
        "configs/local",
    )
    assert all(not (ROOT / relative).exists() for relative in retired)


def test_retired_jenkins_helpers_and_metadata_are_absent() -> None:
    retired = (
        "jenkins/config/workflows.json",
        "jenkins/scripts/lib/kubernetes.sh",
        "jenkins/scripts/lib/port_forward.sh",
        "jenkins/scripts/deploy/kfp_version.sh",
        "jenkins/scripts/deploy/rebalance_ml_node_pool.sh",
        "jenkins/scripts/test/node_placement.sh",
    )
    assert all(not (ROOT / relative).exists() for relative in retired)

    runtime_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (ROOT / "jenkins").rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".sh", ".json", ".groovy", ".Jenkinsfile"}
    )
    for retired_name in (
        "workflowChecks",
        "build_image_single",
        "gcp_production_preflight",
        "with_file_lock",
        "run_node_rebalance_if_enabled",
    ):
        assert retired_name not in runtime_text


def test_runtime_has_no_retired_spark_names_or_local_tag() -> None:
    forbidden = (
        "recsys-" + "mlops-spark",
        "recsys-" + "analytics-spark",
    )
    allowed_name_validators = {
        ROOT / "jenkins/python/image_catalog.py",
        ROOT / "Makefile",
    }
    for path in _runtime_files():
        if (
            "tests" in path.parts
            or path in allowed_name_validators
            or path.suffix
            not in {
                ".py",
                ".sh",
                ".json",
                ".yaml",
                ".yml",
                ".md",
                "",
            }
        ):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert all(token not in text for token in forbidden), path
        if path.name not in {
            "validate_pipeline_package.py",
            "kfp_package.sh",
            "runtime.sh",
        }:
            assert ":local" not in text, path


def test_gke_node_pool_upgrade_strategy_matches_quota_and_availability_needs() -> None:
    gke = (ROOT / "infra/terraform/gcp/gke.tf").read_text(encoding="utf-8")
    cpu = gke.split('resource "google_container_node_pool" "cpu"', 1)[1].split(
        'resource "google_container_node_pool" "llm_cpu"', 1
    )[0]
    llm_cpu = gke.split(
        'resource "google_container_node_pool" "llm_cpu"', 1
    )[1].split('resource "google_container_node_pool" "ml_system"', 1)[0]
    assert "max_surge       = 0" in cpu
    assert "max_unavailable = 1" in cpu
    assert "max_surge       = 0" in llm_cpu
    assert "max_unavailable = 1" in llm_cpu
