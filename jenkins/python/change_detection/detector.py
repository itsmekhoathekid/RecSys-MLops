"""Configuration-driven monorepo change detection.

The detector has one responsibility: map changed paths to component flags.
Image fan-out is resolved through images/catalog.json. Build artifacts and
deployment units are resolved by release_plan.py.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jenkins.python.configuration import (
    load_component_config,
    path_matches_rules,
)
from jenkins.python.image_catalog import image_closure, load_catalog
from jenkins.python.release_plan import create_release_plan


ROUTING_FLAGS = (
    "RUN_CI_CONFIG",
    "RUN_COMPONENT_CI",
    "RUN_COMPONENT_BUILD",
    "RUN_COMPONENT_DEPLOY",
    "RUN_PYTHON",
)


@dataclass(frozen=True)
class ClassificationResult:
    flags: dict[str, bool]
    component_names: tuple[str, ...]
    changed_images: tuple[str, ...]
    changed_paths: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    unmapped_paths: tuple[str, ...]


def git_lines(args: list[str]) -> list[str]:
    output = subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL)
    return [line.strip() for line in output.splitlines() if line.strip()]


def current_commit_paths() -> list[str]:
    for args in (
        ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", "HEAD"],
        ["show", "--pretty=format:", "--name-only", "HEAD"],
    ):
        try:
            return list(dict.fromkeys(git_lines(args)))
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    return []


def changed_paths(base_ref: str | None) -> list[str]:
    if base_ref:
        try:
            return git_lines(["diff", "--name-only", f"{base_ref}...HEAD"])
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    try:
        return git_lines(["diff", "--name-only", "HEAD~1", "HEAD"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return current_commit_paths()


def normalize_path(path: str) -> str:
    normalized = path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _glob_match(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _image_for_path(path: str, images: dict[str, dict]) -> str | None:
    for image_name, spec in images.items():
        dockerfile = str(spec["dockerfile"])
        image_directory = str(Path(dockerfile).parent) + "/"
        if path == dockerfile or path.startswith(image_directory):
            return image_name
    return None


def classify_paths(paths: list[str]) -> ClassificationResult:
    config = load_component_config()
    components = config["components"]
    path_groups = config["pathGroups"]
    images = load_catalog()
    flags = {component["flag"]: False for component in components}
    flags.update({name: False for name in ROUTING_FLAGS})

    normalized_paths = tuple(
        dict.fromkeys(normalize_path(path) for path in paths if normalize_path(path))
    )
    ignored: list[str] = []
    unmapped: list[str] = []
    direct_component_names: set[str] = set()
    directly_changed_images: set[str] = set()

    for path in normalized_paths:
        if _glob_match(path, config["globalExcludes"]):
            ignored.append(path)
            continue

        matched = False
        if path_matches_rules(
            path,
            config["ciConfiguration"]["changeDetection"],
            path_groups,
        ):
            flags["RUN_CI_CONFIG"] = True
            matched = True

        if path in {"images/catalog.json", "images/catalog.schema.json"}:
            flags["RUN_CI_CONFIG"] = True
            direct_component_names.update(component["name"] for component in components)
            matched = True
        else:
            image_name = _image_for_path(path, images)
            if image_name:
                directly_changed_images.add(image_name)
                matched = True

        for component in components:
            if path_matches_rules(path, component["changeDetection"], path_groups):
                direct_component_names.add(component["name"])
                matched = True

        if not matched:
            unmapped.append(path)

    if directly_changed_images:
        for component in components:
            closure = image_closure(component["buildImages"], images)
            if closure & directly_changed_images:
                direct_component_names.add(component["name"])

    ordered_names = tuple(
        component["name"]
        for component in components
        if component["name"] in direct_component_names
    )
    for component in components:
        if component["name"] in direct_component_names:
            flags[component["flag"]] = True
    plan = create_release_plan(
        list(ordered_names),
        changed_images=[
            name for name in images if name in directly_changed_images
        ],
        changed_paths=list(normalized_paths),
    )
    if ordered_names:
        flags["RUN_COMPONENT_CI"] = True
        flags["RUN_PYTHON"] = True
    flags["RUN_COMPONENT_BUILD"] = bool(
        plan["buildImages"] or plan["buildArtifacts"]
    )
    flags["RUN_COMPONENT_DEPLOY"] = bool(plan["deployUnits"])

    return ClassificationResult(
        flags=flags,
        component_names=ordered_names,
        changed_images=tuple(name for name in images if name in directly_changed_images),
        changed_paths=normalized_paths,
        ignored_paths=tuple(ignored),
        unmapped_paths=tuple(unmapped),
    )


def render_environment(result: ClassificationResult) -> str:
    lines = [
        f"{name}={'true' if value else 'false'}"
        for name, value in sorted(result.flags.items())
    ]
    lines.extend(
        (
            f"CHANGED_COMPONENTS={','.join(result.component_names) if result.component_names else 'unchanged'}",
            f"CHANGED_IMAGES={','.join(result.changed_images)}",
            f"CHANGED_PATHS_COUNT={len(result.changed_paths)}",
            f"IGNORED_PATHS_COUNT={len(result.ignored_paths)}",
            f"UNMAPPED_PATHS_COUNT={len(result.unmapped_paths)}",
            f"UNMAPPED_PATHS={'|'.join(result.unmapped_paths)}",
        )
    )
    return "\n".join(lines)


def write_release_plan(result: ClassificationResult, path: Path, commit: str) -> None:
    plan = create_release_plan(
        list(result.component_names),
        changed_images=list(result.changed_images),
        changed_paths=list(result.changed_paths),
        commit=commit,
    )
    path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Map changed paths to RecSys component flags and a release plan."
    )
    parser.add_argument("--base-ref", default="")
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Classify an explicit path instead of reading git diff.",
    )
    parser.add_argument("--plan-output", default=".ci-release-plan.json")
    parser.add_argument("--commit", default="")
    args = parser.parse_args()

    paths = args.path or changed_paths(args.base_ref or None)
    result = classify_paths(paths)
    print(render_environment(result))
    if result.unmapped_paths:
        print(
            "ERROR: Unmapped runtime path(s). Add changeDetection rules: "
            + ", ".join(result.unmapped_paths)
        )
        return 2
    commit = args.commit
    if not commit:
        try:
            commit = git_lines(["rev-parse", "HEAD"])[0]
        except (IndexError, subprocess.CalledProcessError, FileNotFoundError):
            commit = ""
    write_release_plan(result, Path(args.plan_output), commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
