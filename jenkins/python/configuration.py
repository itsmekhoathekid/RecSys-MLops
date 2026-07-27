from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "jenkins" / "config"
MIGRATION_POLICIES = {"none", "expand-only", "reversible"}
REQUIRED_COMPONENT_FIELDS = {
    "name",
    "flag",
    "label",
    "ciProfile",
    "buildProfile",
    "deployProfile",
    "testProfile",
    "deployOrder",
    "migrationPolicy",
}
REQUIRED_GCP_FIELDS = {
    "projectId",
    "region",
    "zone",
    "cluster",
    "context",
    "imageRegistry",
}
REQUIRED_CI_PROFILE_FIELDS = {"projectPath", "lockFile", "pythonVersion"}


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def load_components(path: Path = CONFIG_DIR / "components.json") -> list[dict[str, Any]]:
    payload = read_json(path)
    if payload.get("version") != 1:
        raise ValueError("components.json version must be 1")
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("components.json must contain a non-empty components list")

    seen: dict[str, set[Any]] = {"name": set(), "flag": set(), "label": set(), "deployOrder": set()}
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("each component must be an object")
        missing = REQUIRED_COMPONENT_FIELDS - component.keys()
        if missing:
            raise ValueError(f"component is missing fields: {sorted(missing)}")
        for field, values in seen.items():
            value = component[field]
            if value in values:
                raise ValueError(f"duplicate component {field}: {value}")
            values.add(value)
        if component["migrationPolicy"] not in MIGRATION_POLICIES:
            raise ValueError(
                f"invalid migrationPolicy for {component['name']}: {component['migrationPolicy']}"
            )
    return components


def load_gcp_production(path: Path = CONFIG_DIR / "gcp-production.json") -> dict[str, str]:
    payload = read_json(path)
    missing = REQUIRED_GCP_FIELDS - payload.keys()
    if missing:
        raise ValueError(f"gcp-production.json is missing fields: {sorted(missing)}")
    result = {field: str(payload[field]).strip() for field in REQUIRED_GCP_FIELDS}
    if any(not value for value in result.values()):
        raise ValueError("gcp-production.json fields must not be empty")
    expected_registry = f"{result['region']}-docker.pkg.dev/{result['projectId']}/recsys"
    if result["imageRegistry"] != expected_registry:
        raise ValueError(
            f"production registry must be {expected_registry}, got {result['imageRegistry']}"
        )
    expected_context = (
        f"gke_{result['projectId']}_{result['zone']}_{result['cluster']}"
    )
    if result["context"] != expected_context:
        raise ValueError(
            f"production context must be {expected_context}, got {result['context']}"
        )
    return result


def load_ci_environments(
    path: Path = CONFIG_DIR / "ci-environments.json",
) -> dict[str, dict[str, str]]:
    payload = read_json(path)
    if payload.get("version") != 1:
        raise ValueError("ci-environments.json version must be 1")
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("ci-environments.json must contain profiles")

    result: dict[str, dict[str, str]] = {}
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name or not isinstance(profile, dict):
            raise ValueError("each CI environment profile must be a named object")
        missing = REQUIRED_CI_PROFILE_FIELDS - profile.keys()
        if missing:
            raise ValueError(f"CI profile {name} is missing fields: {sorted(missing)}")
        normalized = {
            field: str(profile[field]).strip() for field in REQUIRED_CI_PROFILE_FIELDS
        }
        if any(not value for value in normalized.values()):
            raise ValueError(f"CI profile {name} fields must not be empty")
        project_path = ROOT / normalized["projectPath"]
        lock_file = ROOT / normalized["lockFile"]
        if not (project_path / "pyproject.toml").is_file():
            raise ValueError(f"CI profile {name} project is missing pyproject.toml")
        if not lock_file.is_file():
            raise ValueError(f"CI profile {name} lock file does not exist")
        result[name] = normalized

    unknown_profiles = {
        component["ciProfile"] for component in load_components()
    } - result.keys()
    if unknown_profiles:
        raise ValueError(
            f"components reference unknown CI profiles: {sorted(unknown_profiles)}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or query Jenkins CI/CD configuration.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("components-tsv")
    profiles_parser = subparsers.add_parser("ci-profiles")
    profiles_parser.add_argument("--components", required=True)
    profile_parser = subparsers.add_parser("ci-profile")
    profile_parser.add_argument("name")
    component_parser = subparsers.add_parser("component")
    component_parser.add_argument("name")
    gcp_parser = subparsers.add_parser("gcp")
    gcp_parser.add_argument("field", choices=sorted(REQUIRED_GCP_FIELDS))
    args = parser.parse_args()

    if args.command == "validate":
        load_components()
        load_gcp_production()
        load_ci_environments()
        return 0
    if args.command == "components-tsv":
        for component in load_components():
            print(
                "\t".join(
                    (
                        component["flag"],
                        component["name"],
                        component["label"],
                    )
                )
            )
        return 0
    if args.command == "ci-profiles":
        components = {item["name"]: item for item in load_components()}
        requested = [
            token.strip()
            for token in args.components.split(",")
            if token.strip() and token.strip() != "ci_config"
        ]
        unknown = sorted(set(requested) - components.keys())
        if unknown:
            raise SystemExit(f"unknown component(s): {', '.join(unknown)}")
        profiles = {
            components[component]["ciProfile"] for component in requested
        }
        for profile in load_ci_environments():
            if profile in profiles:
                print(profile)
        return 0
    if args.command == "ci-profile":
        profiles = load_ci_environments()
        if args.name not in profiles:
            raise SystemExit(f"unknown CI profile: {args.name}")
        print(json.dumps(profiles[args.name], sort_keys=True))
        return 0
    if args.command == "component":
        components = {item["name"]: item for item in load_components()}
        if args.name not in components:
            raise SystemExit(f"unknown component: {args.name}")
        print(json.dumps(components[args.name], sort_keys=True))
        return 0
    if args.command == "gcp":
        print(load_gcp_production()[args.field])
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
