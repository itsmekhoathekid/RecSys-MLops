"""Configuration-driven, single-pass Jenkins release planning."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jenkins.python.configuration import load_component_config, path_matches_rules
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
class ChangedFile:
    status: str
    path: str


@dataclass(frozen=True)
class DetectionOutcome:
    flags: dict[str, bool]
    component_names: tuple[str, ...]
    changed_images: tuple[str, ...]
    changed_paths: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    unmapped_paths: tuple[str, ...]
    deleted_unmapped_paths: tuple[str, ...]
    release_plan: dict[str, object]


def _parse_name_status(payload: bytes) -> list[ChangedFile]:
    tokens = [
        token
        for token in payload.decode("utf-8", errors="surrogateescape").split("\0")
        if token
    ]
    changes: list[ChangedFile] = []
    index = 0
    while index < len(tokens):
        status_token = tokens[index]
        index += 1
        status = status_token[:1]
        if status in {"R", "C"}:
            if index + 1 >= len(tokens):
                raise ValueError(f"invalid git name-status payload near {status_token}")
            old_path, new_path = tokens[index], tokens[index + 1]
            index += 2
            if status == "R":
                changes.append(ChangedFile("D", old_path))
            changes.append(ChangedFile("A", new_path))
            continue
        if index >= len(tokens):
            raise ValueError(f"invalid git name-status payload near {status_token}")
        changes.append(ChangedFile(status, tokens[index]))
        index += 1
    return changes


def _git_name_status(args: list[str]) -> list[ChangedFile]:
    output = subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)
    return _parse_name_status(output)


def current_commit_changes() -> list[ChangedFile]:
    for args in (
        [
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-status",
            "-z",
            "-r",
            "-m",
            "HEAD",
        ],
        ["show", "--pretty=format:", "--name-status", "-z", "HEAD"],
    ):
        try:
            return _git_name_status(args)
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            continue
    return []


def changed_files(base_ref: str | None) -> list[ChangedFile]:
    if base_ref:
        try:
            return _git_name_status(
                ["diff", "--name-status", "-z", f"{base_ref}...HEAD"]
            )
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
            pass
    try:
        return _git_name_status(["diff", "--name-status", "-z", "HEAD~1", "HEAD"])
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return current_commit_changes()


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


def _forced_selection(
    value: str, components: list[dict]
) -> tuple[tuple[str, ...], bool]:
    requested = [token.strip().lower() for token in value.split(",") if token.strip()]
    force_ci_config = "ci_config" in requested
    requested = [token for token in requested if token != "ci_config"]
    by_token: dict[str, str] = {}
    for component in components:
        tokens = {
            component["name"],
            component["flag"].lower().removeprefix("run_"),
            re.sub(r"[^a-z0-9]+", "_", component["label"].lower()).strip("_"),
        }
        for token in tokens:
            by_token[token] = component["name"]
    unknown = sorted(set(requested) - by_token.keys())
    if unknown:
        raise ValueError(f"unknown FORCE_COMPONENTS token(s): {', '.join(unknown)}")
    selected = {by_token[token] for token in requested}
    return (
        tuple(
            component["name"]
            for component in components
            if component["name"] in selected
        ),
        force_ci_config,
    )


def detect_changed_components(
    changes: list[ChangedFile],
    *,
    commit: str = "",
    forced_components: str = "",
    forced_components_mode: str = "union",
) -> DetectionOutcome:
    config = load_component_config()
    components = config["components"]
    path_groups = config["pathGroups"]
    images = load_catalog()
    flags = {component["flag"]: False for component in components}
    flags.update({name: False for name in ROUTING_FLAGS})

    normalized_changes = tuple(
        ChangedFile(change.status[:1].upper(), normalize_path(change.path))
        for change in changes
        if normalize_path(change.path)
    )
    ignored: list[str] = []
    unmapped: list[str] = []
    deleted_unmapped: list[str] = []
    directly_changed_images: set[str] = set()

    if forced_components_mode not in {"union", "replace"}:
        raise ValueError(
            "FORCE_COMPONENTS_MODE must be one of: union, replace"
        )

    if forced_components.strip() and forced_components_mode == "replace":
        ordered_names, force_ci_config = _forced_selection(
            forced_components, components
        )
        flags["RUN_CI_CONFIG"] = force_ci_config
    else:
        direct_component_names: set[str] = set()
        for change in normalized_changes:
            path = change.path
            if _glob_match(path, config["globalExcludes"]):
                ignored.append(path)
                continue

            matched = False
            if path_matches_rules(
                path, config["ciConfiguration"]["changeDetection"], path_groups
            ):
                flags["RUN_CI_CONFIG"] = True
                matched = True

            if path in {"images/catalog.json", "images/catalog.schema.json"}:
                flags["RUN_CI_CONFIG"] = True
                direct_component_names.update(
                    component["name"] for component in components
                )
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
                if change.status == "D":
                    deleted_unmapped.append(path)
                else:
                    unmapped.append(path)

        if directly_changed_images:
            for component in components:
                if (
                    image_closure(component["buildImages"], images)
                    & directly_changed_images
                ):
                    direct_component_names.add(component["name"])
        ordered_names = tuple(
            component["name"]
            for component in components
            if component["name"] in direct_component_names
        )
        if forced_components.strip():
            forced_names, force_ci_config = _forced_selection(
                forced_components, components
            )
            selected_names = set(ordered_names) | set(forced_names)
            ordered_names = tuple(
                component["name"]
                for component in components
                if component["name"] in selected_names
            )
            flags["RUN_CI_CONFIG"] = (
                flags["RUN_CI_CONFIG"] or force_ci_config
            )

    for component in components:
        if component["name"] in ordered_names:
            flags[component["flag"]] = True
    changed_image_names = tuple(
        name for name in images if name in directly_changed_images
    )
    release_plan = create_release_plan(
        list(ordered_names),
        changed_images=list(changed_image_names),
        changed_paths=(
            []
            if forced_components.strip() and forced_components_mode == "replace"
            else [change.path for change in normalized_changes]
        ),
        commit=commit,
    )
    if ordered_names:
        flags["RUN_COMPONENT_CI"] = True
        flags["RUN_PYTHON"] = True
    flags["RUN_COMPONENT_BUILD"] = bool(
        release_plan["buildImages"] or release_plan["buildArtifacts"]
    )
    flags["RUN_COMPONENT_DEPLOY"] = bool(release_plan["deployUnits"])

    return DetectionOutcome(
        flags=flags,
        component_names=ordered_names,
        changed_images=changed_image_names,
        changed_paths=tuple(change.path for change in normalized_changes),
        ignored_paths=tuple(dict.fromkeys(ignored)),
        unmapped_paths=tuple(dict.fromkeys(unmapped)),
        deleted_unmapped_paths=tuple(dict.fromkeys(deleted_unmapped)),
        release_plan=release_plan,
    )


def render_jenkins_environment(result: DetectionOutcome) -> str:
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
            f"DELETED_UNMAPPED_PATHS_COUNT={len(result.deleted_unmapped_paths)}",
            f"DELETED_UNMAPPED_PATHS={'|'.join(result.deleted_unmapped_paths)}",
        )
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Map changed paths to Jenkins flags and one immutable release plan."
    )
    parser.add_argument("--base-ref", default="")
    parser.add_argument("--path", action="append", default=[])
    parser.add_argument("--force-components", default="")
    parser.add_argument(
        "--force-components-mode",
        choices=("union", "replace"),
        default="union",
        help="Union forced components with path detection, or replace path detection.",
    )
    parser.add_argument("--plan-output", default=".ci-release-plan.json")
    parser.add_argument("--commit", default="")
    args = parser.parse_args()

    commit = args.commit
    if not commit:
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            commit = ""
    changes = (
        [ChangedFile("M", path) for path in args.path]
        if args.path
        else changed_files(args.base_ref or None)
    )
    try:
        result = detect_changed_components(
            changes,
            commit=commit,
            forced_components=args.force_components,
            forced_components_mode=args.force_components_mode,
        )
    except ValueError as error:
        print(f"ERROR: {error}")
        return 2
    print(render_jenkins_environment(result))
    if result.unmapped_paths:
        print(
            "ERROR: Unmapped active runtime path(s). Add changeDetection rules: "
            + ", ".join(result.unmapped_paths)
        )
        return 2
    Path(args.plan_output).write_text(
        json.dumps(result.release_plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
