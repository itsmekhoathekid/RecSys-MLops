from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from jenkins.python.image_catalog import (  # noqa: E402
    dependency_build_args,
    dependency_order,
    load_catalog,
)
from jenkins.python.configuration import load_components  # noqa: E402
from jenkins.python.release_plan import create_release_plan  # noqa: E402


def _catalog_payload() -> dict:
    return json.loads((ROOT / "images/catalog.json").read_text(encoding="utf-8"))


def test_catalog_contains_exactly_fifteen_images_and_one_spark() -> None:
    images = load_catalog()

    assert len(images) == 15
    assert {name for name in images if name.endswith("-spark")} == {"recsys-spark"}
    assert all(spec["context"] == "." for spec in images.values())
    assert all((ROOT / spec["dockerfile"]).is_file() for spec in images.values())


def test_internal_dependencies_are_topologically_ordered() -> None:
    assert dependency_order("recsys-data-ingestion") == [
        "recsys-base-python",
        "recsys-data-ingestion",
    ]
    assert dependency_build_args("recsys-data-ingestion", "abc123") == [
        "RECSYS_BASE_IMAGE=recsys-base-python:abc123"
    ]


def test_catalog_rejects_legacy_spark_images(tmp_path: Path) -> None:
    payload = _catalog_payload()
    payload["images"]["recsys-mlops-spark"] = payload["images"].pop(
        "recsys-analytics-dbt"
    )
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden legacy images"):
        load_catalog(catalog_path)


def test_every_catalog_image_is_reachable_from_a_component() -> None:
    # load_catalog performs closure, unknown dependency, and cycle validation.
    assert set(load_catalog()) == set(_catalog_payload()["images"])


def test_full_release_plan_builds_every_image_once_in_topological_order() -> None:
    images = load_catalog()
    plan = create_release_plan([component["name"] for component in load_components()])

    assert len(plan["buildImages"]) == len(set(plan["buildImages"])) == 15
    position = {name: index for index, name in enumerate(plan["buildImages"])}
    for name, spec in images.items():
        for dependency in spec["dependencies"]:
            assert position[dependency["image"]] < position[name]


def test_release_builder_invokes_each_planned_image_once(tmp_path: Path) -> None:
    plan = create_release_plan([component["name"] for component in load_components()])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    docker_stub = bin_dir / "docker"
    docker_stub.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >>"${DOCKER_LOG}"\n',
        encoding="utf-8",
    )
    docker_stub.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "IMAGE_MANIFEST_DIR": str(tmp_path / "manifest"),
            "REPORTS_DIR": str(tmp_path / "reports"),
            "IMAGE_PUSH_REGISTRY": "registry.example.invalid/recsys",
            "IMAGE_TAG": "a" * 40,
            "PUBLISH_IMAGES": "0",
            "REQUIRE_GCP_ARTIFACT_REGISTRY": "0",
            "CONTAINER_SCAN_ENABLED": "0",
        }
    )
    subprocess.run(
        [
            "bash",
            "jenkins/scripts/entrypoints/release_build_publish.sh",
            str(plan_path),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    builds = [
        line
        for line in docker_log.read_text().splitlines()
        if line.startswith("build ")
    ]
    assert len(builds) == 15
    for image_name in plan["buildImages"]:
        assert sum(f"-t {image_name}:" in line for line in builds) == 1
