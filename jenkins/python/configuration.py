from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CONFIG_DIR = ROOT / "jenkins" / "config"
MIGRATION_POLICIES = {"none", "expand-only", "reversible"}
RULE_FIELDS = {"groups", "prefixes", "files", "globs", "exclude"}
REQUIRED_COMPONENT_FIELDS = {
    "name",
    "flag",
    "label",
    "ciProfile",
    "changeDetection",
    "buildImages",
    "buildArtifacts",
    "verifyDependsOn",
    "migrationPolicy",
}
SUPPORTED_BUILD_ARTIFACTS = {"kubeflow-bst"}
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


def _validate_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(f"{label} contains duplicates")
    return value


def _validate_rules(
    rules: Any,
    label: str,
    path_groups: dict[str, dict[str, Any]],
    *,
    require_rule: bool = True,
) -> dict[str, Any]:
    if not isinstance(rules, dict):
        raise ValueError(f"{label} must be an object")
    unknown_fields = set(rules) - RULE_FIELDS
    if unknown_fields:
        raise ValueError(
            f"{label} contains unsupported fields: {sorted(unknown_fields)}"
        )
    if require_rule and not any(
        rules.get(field) for field in RULE_FIELDS - {"exclude"}
    ):
        raise ValueError(f"{label} must contain at least one positive rule")
    for field in RULE_FIELDS:
        if field in rules:
            _validate_string_list(rules[field], f"{label}.{field}")
    unknown_groups = set(rules.get("groups", [])) - path_groups.keys()
    if unknown_groups:
        raise ValueError(
            f"{label} references unknown path groups: {sorted(unknown_groups)}"
        )
    return rules


def _validate_rule_paths(rules: dict[str, Any], label: str) -> None:
    for relative_path in rules.get("files", []):
        if not (ROOT / relative_path).is_file():
            raise ValueError(f"{label}.files path does not exist: {relative_path}")
        if relative_path not in _tracked_paths():
            raise ValueError(
                f"{label}.files path is not tracked by Git: {relative_path}"
            )
    for relative_path in rules.get("prefixes", []):
        if not (ROOT / relative_path).exists():
            raise ValueError(f"{label}.prefixes path does not exist: {relative_path}")
        prefix = relative_path.rstrip("/") + "/"
        if not any(
            path == relative_path or path.startswith(prefix)
            for path in _tracked_paths()
        ):
            raise ValueError(
                f"{label}.prefixes path has no Git-tracked files: {relative_path}"
            )
    for pattern in rules.get("globs", []):
        matched_paths = {
            str(path.relative_to(ROOT)) for path in ROOT.glob(pattern) if path.is_file()
        }
        if not matched_paths:
            raise ValueError(f"{label}.globs pattern matches no files: {pattern}")
        if not matched_paths.intersection(_tracked_paths()):
            raise ValueError(
                f"{label}.globs pattern matches no Git-tracked files: {pattern}"
            )


@lru_cache(maxsize=1)
def _tracked_paths() -> frozenset[str]:
    if not (ROOT / ".git").exists():
        return frozenset(
            str(path.relative_to(ROOT)) for path in ROOT.rglob("*") if path.is_file()
        )
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return frozenset(
        item.decode("utf-8") for item in completed.stdout.split(b"\0") if item
    )


def load_component_config(
    path: Path = CONFIG_DIR / "components.json",
) -> dict[str, Any]:
    payload = read_json(path)
    if payload.get("version") != 4:
        raise ValueError("components.json version must be 4")
    global_excludes = _validate_string_list(
        payload.get("globalExcludes", []), "components.json globalExcludes"
    )
    path_groups = payload.get("pathGroups", {})
    if not isinstance(path_groups, dict):
        raise ValueError("components.json pathGroups must be an object")
    for name, rules in path_groups.items():
        if not isinstance(name, str) or not name:
            raise ValueError("path group names must be non-empty strings")
        _validate_rules(rules, f"path group {name}", {}, require_rule=True)
        _validate_rule_paths(rules, f"path group {name}")

    ci_config = payload.get("ciConfiguration")
    if not isinstance(ci_config, dict) or ci_config.get("flag") != "RUN_CI_CONFIG":
        raise ValueError("components.json ciConfiguration.flag must be RUN_CI_CONFIG")
    _validate_rules(
        ci_config.get("changeDetection"),
        "ciConfiguration.changeDetection",
        path_groups,
    )
    _validate_rule_paths(
        ci_config["changeDetection"], "ciConfiguration.changeDetection"
    )

    components = payload.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError("components.json must contain a non-empty components list")
    seen: dict[str, set[Any]] = {"name": set(), "flag": set(), "label": set()}
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("each component must be an object")
        missing = REQUIRED_COMPONENT_FIELDS - component.keys()
        if missing:
            raise ValueError(f"component is missing fields: {sorted(missing)}")
        unknown = set(component) - REQUIRED_COMPONENT_FIELDS
        if unknown:
            raise ValueError(
                f"component {component.get('name', '<unknown>')} contains unsupported fields: "
                f"{sorted(unknown)}"
            )
        for field, values in seen.items():
            value = component[field]
            if value in values:
                raise ValueError(f"duplicate component {field}: {value}")
            values.add(value)
        if not component["flag"].startswith("RUN_"):
            raise ValueError(f"component {component['name']} flag must start with RUN_")
        _validate_rules(
            component["changeDetection"],
            f"component {component['name']} changeDetection",
            path_groups,
        )
        _validate_rule_paths(
            component["changeDetection"],
            f"component {component['name']} changeDetection",
        )
        _validate_string_list(
            component["buildImages"], f"component {component['name']} buildImages"
        )
        _validate_string_list(
            component["buildArtifacts"], f"component {component['name']} buildArtifacts"
        )
        _validate_string_list(
            component["verifyDependsOn"],
            f"component {component['name']} verifyDependsOn",
        )
        unknown_artifacts = set(component["buildArtifacts"]) - SUPPORTED_BUILD_ARTIFACTS
        if unknown_artifacts:
            raise ValueError(
                f"component {component['name']} references unsupported build artifacts: "
                f"{sorted(unknown_artifacts)}"
            )
        if component["migrationPolicy"] not in MIGRATION_POLICIES:
            raise ValueError(
                f"invalid migrationPolicy for {component['name']}: "
                f"{component['migrationPolicy']}"
            )
    component_names = {component["name"] for component in components}
    for component in components:
        unknown_dependencies = set(component["verifyDependsOn"]) - component_names
        if unknown_dependencies:
            raise ValueError(
                f"component {component['name']} verifyDependsOn references unknown "
                f"components: {sorted(unknown_dependencies)}"
            )
        if component["name"] in component["verifyDependsOn"]:
            raise ValueError(
                f"component {component['name']} cannot verifyDependsOn itself"
            )
    visiting: set[str] = set()
    visited: set[str] = set()
    by_name = {component["name"]: component for component in components}

    def visit_verification(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"component verification dependency cycle includes {name}")
        visiting.add(name)
        for dependency in by_name[name]["verifyDependsOn"]:
            visit_verification(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in by_name:
        visit_verification(name)
    payload["globalExcludes"] = global_excludes
    return payload


def load_components(
    path: Path = CONFIG_DIR / "components.json",
) -> list[dict[str, Any]]:
    return load_component_config(path)["components"]


def expanded_change_rules(
    rules: dict[str, Any], path_groups: dict[str, dict[str, Any]]
) -> dict[str, list[str]]:
    expanded = {field: list(rules.get(field, [])) for field in RULE_FIELDS - {"groups"}}
    for group_name in rules.get("groups", []):
        group = path_groups[group_name]
        for field in RULE_FIELDS - {"groups"}:
            expanded[field].extend(group.get(field, []))
    return {field: list(dict.fromkeys(values)) for field, values in expanded.items()}


def path_matches_rules(
    path: str, rules: dict[str, Any], path_groups: dict[str, dict[str, Any]]
) -> bool:
    expanded = expanded_change_rules(rules, path_groups)
    if any(fnmatch.fnmatchcase(path, pattern) for pattern in expanded["exclude"]):
        return False
    return (
        path in expanded["files"]
        or any(path.startswith(prefix) for prefix in expanded["prefixes"])
        or any(fnmatch.fnmatchcase(path, pattern) for pattern in expanded["globs"])
    )


def load_gcp_production(
    path: Path = CONFIG_DIR / "gcp-production.json",
) -> dict[str, str]:
    payload = read_json(path)
    missing = REQUIRED_GCP_FIELDS - payload.keys()
    if missing:
        raise ValueError(f"gcp-production.json is missing fields: {sorted(missing)}")
    result = {field: str(payload[field]).strip() for field in REQUIRED_GCP_FIELDS}
    if any(not value for value in result.values()):
        raise ValueError("gcp-production.json fields must not be empty")
    expected_registry = (
        f"{result['region']}-docker.pkg.dev/{result['projectId']}/recsys"
    )
    if result["imageRegistry"] != expected_registry:
        raise ValueError(
            f"production registry must be {expected_registry}, got {result['imageRegistry']}"
        )
    expected_context = f"gke_{result['projectId']}_{result['zone']}_{result['cluster']}"
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
        if not (ROOT / normalized["projectPath"] / "pyproject.toml").is_file():
            raise ValueError(f"CI profile {name} project is missing pyproject.toml")
        if not (ROOT / normalized["lockFile"]).is_file():
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
    parser = argparse.ArgumentParser(
        description="Validate or query Jenkins CI/CD configuration."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("components-tsv")
    profiles_parser = subparsers.add_parser("ci-profiles")
    profiles_parser.add_argument("--components", required=True)
    component_parser = subparsers.add_parser("component-profile")
    component_parser.add_argument("name")
    subparsers.add_parser("gcp-tsv")
    gcp_parser = subparsers.add_parser("gcp")
    gcp_parser.add_argument("field", choices=sorted(REQUIRED_GCP_FIELDS))
    args = parser.parse_args()

    if args.command == "validate":
        load_component_config()
        load_gcp_production()
        load_ci_environments()
        from jenkins.python.image_catalog import load_catalog
        from jenkins.python.release_plan import load_deploy_config

        load_catalog()
        load_deploy_config()
        return 0
    if args.command == "components-tsv":
        for component in load_components():
            print("\t".join((component["flag"], component["name"], component["label"])))
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
        requested_profiles = {components[name]["ciProfile"] for name in requested}
        profiles = load_ci_environments()
        for profile in profiles:
            if profile in requested_profiles:
                spec = profiles[profile]
                print(
                    "\t".join(
                        (
                            profile,
                            spec["projectPath"],
                            spec["lockFile"],
                            spec["pythonVersion"],
                        )
                    )
                )
        return 0
    if args.command == "component-profile":
        components = {item["name"]: item for item in load_components()}
        if args.name not in components:
            raise SystemExit(f"unknown component: {args.name}")
        print(components[args.name]["ciProfile"])
        return 0
    if args.command == "gcp":
        print(load_gcp_production()[args.field])
        return 0
    if args.command == "gcp-tsv":
        target = load_gcp_production()
        print("\t".join(target[field] for field in sorted(REQUIRED_GCP_FIELDS)))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
