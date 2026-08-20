from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = ROOT / "images" / "catalog.json"
COMPONENTS_PATH = ROOT / "jenkins" / "config" / "components.json"
FORBIDDEN_IMAGES = {"recsys-mlops-spark", "recsys-analytics-spark"}
REQUIRED_IMAGE_FIELDS = {"dockerfile", "context", "dependencies"}
REQUIRED_DEPENDENCY_FIELDS = {"image", "buildArg"}


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, dict[str, Any]]:
    payload = _read_object(path)
    if payload.get("version") != 1:
        raise ValueError("image catalog version must be 1")
    images = payload.get("images")
    if not isinstance(images, dict):
        raise ValueError("image catalog must contain an images object")
    if not images:
        raise ValueError("image catalog must contain at least one image")
    if FORBIDDEN_IMAGES.intersection(images):
        raise ValueError(
            f"forbidden legacy images: {sorted(FORBIDDEN_IMAGES.intersection(images))}"
        )
    for image_name, spec in images.items():
        if not isinstance(image_name, str) or not image_name.startswith("recsys-"):
            raise ValueError(f"invalid image name: {image_name!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"image {image_name} spec must be an object")
        if set(spec) != REQUIRED_IMAGE_FIELDS:
            raise ValueError(
                f"image {image_name} fields must be {sorted(REQUIRED_IMAGE_FIELDS)}"
            )
        dockerfile = ROOT / str(spec["dockerfile"])
        context = ROOT / str(spec["context"])
        if not dockerfile.is_file():
            raise ValueError(
                f"image {image_name} Dockerfile does not exist: {dockerfile}"
            )
        if not context.is_dir():
            raise ValueError(f"image {image_name} context does not exist: {context}")
        if spec["context"] != ".":
            raise ValueError(f"image {image_name} context must be repository root")
        dependencies = spec["dependencies"]
        if not isinstance(dependencies, list):
            raise ValueError(f"image {image_name} dependencies must be a list")
        seen_args: set[str] = set()
        for dependency in dependencies:
            if (
                not isinstance(dependency, dict)
                or set(dependency) != REQUIRED_DEPENDENCY_FIELDS
            ):
                raise ValueError(
                    f"image {image_name} dependencies require image and buildArg"
                )
            dependency_image = dependency["image"]
            build_arg = dependency["buildArg"]
            if dependency_image not in images:
                raise ValueError(
                    f"image {image_name} references unknown dependency {dependency_image}"
                )
            if not isinstance(build_arg, str) or not build_arg:
                raise ValueError(
                    f"image {image_name} has an invalid dependency buildArg"
                )
            if build_arg in seen_args:
                raise ValueError(
                    f"image {image_name} has duplicate dependency buildArg {build_arg}"
                )
            seen_args.add(build_arg)
    _validate_acyclic(images)
    _validate_component_coverage(images)
    return images


def _dependency_names(spec: dict[str, Any]) -> Iterable[str]:
    for dependency in spec["dependencies"]:
        yield str(dependency["image"])


def _validate_acyclic(images: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(image_name: str) -> None:
        if image_name in visiting:
            raise ValueError(f"image dependency cycle contains {image_name}")
        if image_name in visited:
            return
        visiting.add(image_name)
        for dependency in _dependency_names(images[image_name]):
            visit(dependency)
        visiting.remove(image_name)
        visited.add(image_name)

    for image_name in images:
        visit(image_name)


def dependency_order(
    image_name: str,
    images: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    images = images or load_catalog()
    if image_name not in images:
        raise ValueError(f"unknown image: {image_name}")
    result: list[str] = []
    visited: set[str] = set()

    def visit(current: str) -> None:
        if current in visited:
            return
        for dependency in _dependency_names(images[current]):
            visit(dependency)
        visited.add(current)
        result.append(current)

    visit(image_name)
    return result


def dependency_build_args(
    image_name: str,
    tag: str,
    images: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    images = images or load_catalog()
    if image_name not in images:
        raise ValueError(f"unknown image: {image_name}")
    return [
        f"{dependency['buildArg']}={dependency['image']}:{tag}"
        for dependency in images[image_name]["dependencies"]
    ]


def image_closure(
    image_names: Iterable[str],
    images: dict[str, dict[str, Any]],
) -> set[str]:
    result: set[str] = set()
    for image_name in image_names:
        result.update(dependency_order(image_name, images))
    return result


def _validate_component_coverage(images: dict[str, dict[str, Any]]) -> None:
    components_payload = _read_object(COMPONENTS_PATH)
    components = components_payload.get("components")
    if not isinstance(components, list):
        raise ValueError("components.json must contain a components list")
    referenced: list[str] = []
    for component in components:
        component_images = component.get("buildImages")
        if not isinstance(component_images, list):
            raise ValueError(
                f"component {component.get('name')} must define a buildImages list"
            )
        unknown = set(component_images) - images.keys()
        if unknown:
            raise ValueError(
                f"component {component.get('name')} references unknown images: "
                f"{sorted(unknown)}"
            )
        referenced.extend(component_images)
    uncovered = images.keys() - image_closure(referenced, images)
    if uncovered:
        raise ValueError(
            f"catalog images are not reachable from components: {sorted(uncovered)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and query the RecSys image catalog."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    spec_parser = subparsers.add_parser("spec")
    spec_parser.add_argument("image")
    dependencies_parser = subparsers.add_parser("dependencies")
    dependencies_parser.add_argument("image")
    build_args_parser = subparsers.add_parser("build-args")
    build_args_parser.add_argument("image")
    build_args_parser.add_argument("--tag", required=True)
    build_spec_parser = subparsers.add_parser("build-spec")
    build_spec_parser.add_argument("image")
    build_spec_parser.add_argument("--tag", required=True)
    args = parser.parse_args()

    images = load_catalog()
    if args.command == "validate":
        return 0
    if args.image not in images:
        raise SystemExit(f"unknown image: {args.image}")
    if args.command == "spec":
        print(json.dumps(images[args.image], sort_keys=True))
        return 0
    if args.command == "dependencies":
        for image_name in dependency_order(args.image, images):
            print(image_name)
        return 0
    if args.command == "build-args":
        for build_arg in dependency_build_args(args.image, args.tag, images):
            print(build_arg)
        return 0
    if args.command == "build-spec":
        spec = images[args.image]
        print(f"CONTEXT\t{spec['dockerfile']}\t{spec['context']}")
        for build_arg in dependency_build_args(args.image, args.tag, images):
            print(f"ARG\t{build_arg}")
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
