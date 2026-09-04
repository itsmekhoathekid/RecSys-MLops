from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jenkins.python.configuration import (  # noqa: E402
    CONFIG_DIR,
    ROOT,
    load_components,
    read_json,
)
from jenkins.python.image_catalog import image_closure, load_catalog  # noqa: E402

UNIT_KINDS = {"helm", "kubeflow-package", "jenkins-action", "kubernetes-action"}
REQUIRED_UNIT_FIELDS = {
    "name",
    "kind",
    "release",
    "namespace",
    "components",
    "consumesImages",
    "consumesArtifacts",
    "dependsOn",
}


def _string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicates")
    return value


def load_deploy_config(path: Path = CONFIG_DIR / "deploy-units.json") -> dict[str, Any]:
    payload = read_json(path)
    if payload.get("version") != 2:
        raise ValueError("deploy-units.json version must be 2")
    components = {item["name"] for item in load_components()}
    images = set(load_catalog())
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("deploy-units.json artifacts must be an object")
    for name, spec in artifacts.items():
        if not isinstance(name, str) or not name or not isinstance(spec, dict):
            raise ValueError("artifact definitions must be named objects")
        consumed = _string_list(
            spec.get("consumesImages"), f"artifact {name} consumesImages"
        )
        unknown_images = set(consumed) - images
        if unknown_images:
            raise ValueError(
                f"artifact {name} consumes unknown images: {sorted(unknown_images)}"
            )

    units = payload.get("units")
    if not isinstance(units, list) or not units:
        raise ValueError("deploy-units.json must contain non-empty units")
    names: set[str] = set()
    release_owners: set[tuple[str, str]] = set()
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError("each deploy unit must be an object")
        missing = REQUIRED_UNIT_FIELDS - unit.keys()
        if missing:
            raise ValueError(f"deploy unit is missing fields: {sorted(missing)}")
        allowed = REQUIRED_UNIT_FIELDS | {"requiresExplicitComponent"} | (
            {"chart", "imageValues"} if unit.get("kind") == "helm" else set()
        )
        unknown_fields = set(unit) - allowed
        if unknown_fields:
            raise ValueError(
                f"deploy unit {unit.get('name', '<unknown>')} contains unsupported fields: "
                f"{sorted(unknown_fields)}"
            )
        name = unit["name"]
        if name in names:
            raise ValueError(f"duplicate deploy unit name: {name}")
        names.add(name)
        if unit["kind"] not in UNIT_KINDS:
            raise ValueError(f"deploy unit {name} has unsupported kind: {unit['kind']}")
        owner = (unit["namespace"], unit["release"])
        if owner in release_owners:
            raise ValueError(f"duplicate deploy release owner: {owner}")
        release_owners.add(owner)
        for field in ("components", "consumesImages", "consumesArtifacts", "dependsOn"):
            _string_list(unit[field], f"deploy unit {name} {field}")
        if set(unit["components"]) - components:
            raise ValueError(
                f"deploy unit {name} references unknown components: "
                f"{sorted(set(unit['components']) - components)}"
            )
        if not isinstance(unit.get("requiresExplicitComponent", False), bool):
            raise ValueError(
                f"deploy unit {name} requiresExplicitComponent must be a boolean"
            )
        if set(unit["consumesImages"]) - images:
            raise ValueError(
                f"deploy unit {name} consumes unknown images: "
                f"{sorted(set(unit['consumesImages']) - images)}"
            )
        if set(unit["consumesArtifacts"]) - artifacts.keys():
            raise ValueError(
                f"deploy unit {name} consumes unknown artifacts: "
                f"{sorted(set(unit['consumesArtifacts']) - artifacts.keys())}"
            )
        if unit["kind"] == "helm":
            chart = unit.get("chart")
            if not isinstance(chart, str) or not chart:
                raise ValueError(f"Helm deploy unit {name} requires chart")
            if not (ROOT / chart / "Chart.yaml").is_file():
                raise ValueError(
                    f"Helm deploy unit {name} chart does not exist: {chart}"
                )
            image_values = unit.get("imageValues", {})
            if not isinstance(image_values, dict):
                raise ValueError(
                    f"Helm deploy unit {name} imageValues must be an object"
                )
            if set(image_values) - set(unit["consumesImages"]):
                raise ValueError(
                    f"Helm deploy unit {name} imageValues contains non-consumed images"
                )
            if any(
                not isinstance(value_path, str) or not value_path
                for value_path in image_values.values()
            ):
                raise ValueError(
                    f"Helm deploy unit {name} imageValues paths must be strings"
                )
    by_name = {unit["name"]: unit for unit in units}
    for unit in units:
        unknown_dependencies = set(unit["dependsOn"]) - by_name.keys()
        if unknown_dependencies:
            raise ValueError(
                f"deploy unit {unit['name']} has unknown dependencies: "
                f"{sorted(unknown_dependencies)}"
            )
    topological_order(by_name, set(by_name))
    return payload


def topological_order(
    by_name: dict[str, dict[str, Any]], selected: set[str]
) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[str] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"deploy unit dependency cycle includes {name}")
        visiting.add(name)
        for dependency in by_name[name]["dependsOn"]:
            if dependency in selected:
                visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(name)

    for name in by_name:
        if name in selected:
            visit(name)
    return ordered


def create_release_plan(
    component_names: list[str],
    *,
    changed_images: list[str] | None = None,
    changed_paths: list[str] | None = None,
    commit: str = "",
) -> dict[str, Any]:
    components = {item["name"]: item for item in load_components()}
    unknown = set(component_names) - components.keys()
    if unknown:
        raise ValueError(f"unknown components in release plan: {sorted(unknown)}")
    image_catalog = load_catalog()
    direct_images = list(
        dict.fromkeys(
            image
            for name in component_names
            for image in components[name]["buildImages"]
        )
    )
    direct_images.extend(
        image for image in (changed_images or []) if image not in direct_images
    )
    deploy_trigger_images = image_closure(direct_images, image_catalog)
    deploy_config = load_deploy_config()
    artifact_specs = deploy_config["artifacts"]
    build_artifacts = list(
        dict.fromkeys(
            artifact
            for name in component_names
            for artifact in components[name]["buildArtifacts"]
        )
    )
    for artifact, spec in artifact_specs.items():
        if (
            set(spec["consumesImages"]).issubset(deploy_trigger_images)
            and artifact not in build_artifacts
        ):
            build_artifacts.append(artifact)
    artifact_images = [
        image
        for artifact in build_artifacts
        for image in artifact_specs[artifact]["consumesImages"]
    ]
    closure = image_closure([*direct_images, *artifact_images], image_catalog)
    image_specs = {
        name: {
            "dependsOn": [dependency["image"] for dependency in spec["dependencies"]]
        }
        for name, spec in image_catalog.items()
    }
    build_images = topological_order(image_specs, closure)

    units = deploy_config["units"]
    selected_units = set()
    for unit in units:
        explicitly_selected = bool(
            set(unit["components"]) & set(component_names)
        )
        dependency_triggered = (
            not unit.get("requiresExplicitComponent", False)
            and (
                (
                    not unit["consumesArtifacts"]
                    and bool(set(unit["consumesImages"]) & deploy_trigger_images)
                )
                or bool(set(unit["consumesArtifacts"]) & set(build_artifacts))
            )
        )
        if explicitly_selected or dependency_triggered:
            selected_units.add(unit["name"])
    for unit in units:
        chart = unit.get("chart")
        if chart and any(
            path == chart or path.startswith(chart.rstrip("/") + "/")
            for path in (changed_paths or [])
        ):
            selected_units.add(unit["name"])
    by_name = {unit["name"]: unit for unit in units}
    ordered_units = topological_order(by_name, selected_units)
    return {
        "version": 2,
        "commit": commit,
        "components": component_names,
        "buildImages": build_images,
        "buildArtifacts": build_artifacts,
        "deployUnits": ordered_units,
    }


def load_release_plan(path: Path) -> dict[str, Any]:
    plan = read_json(path)
    if plan.get("version") != 2:
        raise ValueError("release plan version must be 2")
    required = {
        "version",
        "commit",
        "components",
        "buildImages",
        "buildArtifacts",
        "deployUnits",
    }
    if set(plan) != required:
        raise ValueError(f"release plan fields must be {sorted(required)}")
    for field in required - {"version", "commit"}:
        _string_list(plan[field], f"release plan {field}")
    if not isinstance(plan["commit"], str):
        raise ValueError("release plan commit must be a string")
    images = load_catalog()
    selected_images = set(plan["buildImages"])
    unknown_images = selected_images - images.keys()
    if unknown_images:
        raise ValueError(
            f"release plan references unknown build images: {sorted(unknown_images)}"
        )
    image_specs = {
        name: {
            "dependsOn": [dependency["image"] for dependency in spec["dependencies"]]
        }
        for name, spec in images.items()
    }
    missing_dependencies = {
        dependency
        for image_name in selected_images
        for dependency in image_specs[image_name]["dependsOn"]
        if dependency not in selected_images
    }
    if missing_dependencies:
        raise ValueError(
            f"release plan omits image dependencies: {sorted(missing_dependencies)}"
        )
    if plan["buildImages"] != topological_order(image_specs, selected_images):
        raise ValueError(
            "release plan buildImages must be in topological catalog order"
        )
    deploy_config = load_deploy_config()
    unknown_artifacts = set(plan["buildArtifacts"]) - deploy_config["artifacts"].keys()
    if unknown_artifacts:
        raise ValueError(
            f"release plan references unknown artifacts: {sorted(unknown_artifacts)}"
        )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and validate an immutable CI/CD release plan."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    create = subparsers.add_parser("create")
    create.add_argument("--components", required=True)
    create.add_argument("--changed-images", default="")
    create.add_argument("--changed-path", action="append", default=[])
    create.add_argument("--commit", default="")
    create.add_argument("--output", default="")
    deploy_context = subparsers.add_parser("deploy-context")
    deploy_context.add_argument("name")
    deploy_context.add_argument("--plan", required=True)
    plan_units = subparsers.add_parser("plan-units")
    plan_units.add_argument("--plan", required=True)
    plan_units.add_argument(
        "--phase",
        choices=("all", "deploy", "finalize"),
        default="all",
        help="Select workload deployment units or post-verification finalizers.",
    )
    plan_images = subparsers.add_parser("plan-images")
    plan_images.add_argument("--plan", required=True)
    plan_artifacts = subparsers.add_parser("plan-artifacts")
    plan_artifacts.add_argument("--plan", required=True)
    plan_verifications = subparsers.add_parser("plan-verifications")
    plan_verifications.add_argument("--plan", required=True)
    args = parser.parse_args()
    if args.command == "validate":
        load_deploy_config()
        return 0
    if args.command == "create":
        plan = create_release_plan(
            [token for token in args.components.split(",") if token],
            changed_images=[token for token in args.changed_images.split(",") if token],
            changed_paths=args.changed_path,
            commit=args.commit,
        )
        rendered = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        if args.output:
            Path(args.output).write_text(rendered, encoding="utf-8")
        else:
            print(rendered, end="")
        return 0
    if args.command == "deploy-context":
        units = {item["name"]: item for item in load_deploy_config()["units"]}
        if args.name not in units:
            raise SystemExit(f"unknown deploy unit: {args.name}")
        plan = load_release_plan(Path(args.plan))
        unit = units[args.name]
        print(
            "\t".join(
                (
                    "UNIT",
                    unit["kind"],
                    unit["release"],
                    unit["namespace"],
                    unit.get("chart", ""),
                )
            )
        )
        for image_name, value_path in unit.get("imageValues", {}).items():
            print(f"IMAGE\t{image_name}\t{value_path}")
        for component in plan["components"]:
            print(f"SELECTED_COMPONENT\t{component}")
        return 0
    if args.command == "plan-units":
        plan = load_release_plan(Path(args.plan))
        selected = set(plan.get("deployUnits", []))
        units = {item["name"]: item for item in load_deploy_config()["units"]}
        unknown = selected - units.keys()
        if unknown:
            raise SystemExit(
                f"release plan references unknown deploy units: {sorted(unknown)}"
            )
        def is_finalizer(name: str) -> bool:
            unit = units[name]
            return unit["kind"] == "jenkins-action" and name.endswith("-registry")

        phase_selected = {
            name
            for name in selected
            if args.phase == "all"
            or (args.phase == "finalize" and is_finalizer(name))
            or (args.phase == "deploy" and not is_finalizer(name))
        }
        depths: dict[str, int] = {}
        for name in topological_order(units, phase_selected):
            depths[name] = max(
                (
                    depths[dependency] + 1
                    for dependency in units[name]["dependsOn"]
                    if dependency in phase_selected
                ),
                default=0,
            )
            lock_name = f"{units[name]['kind']}:{units[name]['namespace']}:{units[name]['release']}"
            print(f"{depths[name]}\t{name}\t{lock_name}")
        return 0
    if args.command == "plan-images":
        for image_name in load_release_plan(Path(args.plan))["buildImages"]:
            print(image_name)
        return 0
    if args.command == "plan-artifacts":
        for artifact in load_release_plan(Path(args.plan))["buildArtifacts"]:
            print(artifact)
        return 0
    if args.command == "plan-verifications":
        plan = load_release_plan(Path(args.plan))
        selected = set(plan.get("components", []))
        components = {item["name"]: item for item in load_components()}
        unknown = selected - components.keys()
        if unknown:
            raise SystemExit(
                f"release plan references unknown components: {sorted(unknown)}"
            )
        verification_specs = {
            name: {"dependsOn": component["verifyDependsOn"]}
            for name, component in components.items()
        }
        for name in topological_order(verification_specs, selected):
            print(name)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
