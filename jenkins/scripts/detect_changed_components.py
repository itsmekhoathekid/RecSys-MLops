from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


FLAGS = {
    "RUN_API": False,
    "RUN_DATA_GENERATOR": False,
    "RUN_DATA_PLATFORM": False,
    "RUN_FEATURE_STORE": False,
    "RUN_MODEL_PIPELINE": False,
    "RUN_KFP": False,
    "RUN_HELM": False,
    "RUN_DOCKER_BASE": False,
    "RUN_DOCKER_DATA_GENERATOR": False,
    "RUN_DOCKER_DATAFLOW": False,
    "RUN_DOCKER_FEATURE_STORE": False,
    "RUN_DOCKER_TRAINING": False,
    "RUN_PYTHON": False,
}


def git_lines(args: list[str]) -> list[str]:
    output = subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL)
    return [line.strip() for line in output.splitlines() if line.strip()]


def changed_paths(base_ref: str | None) -> list[str]:
    candidates: list[list[str]] = []
    if base_ref:
        candidates.append(["diff", "--name-only", f"{base_ref}...HEAD"])
    candidates.append(["diff", "--name-only", "HEAD~1", "HEAD"])

    for args in candidates:
        try:
            paths = git_lines(args)
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
        if paths:
            return paths

    try:
        return git_lines(["ls-files"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def mark(flags: dict[str, bool], *names: str) -> None:
    for name in names:
        flags[name] = True


def classify(paths: list[str]) -> dict[str, bool]:
    flags = dict(FLAGS)
    for path in paths:
        p = Path(path)
        parts = p.parts

        if path in {"pyproject.toml", "uv.lock"}:
            mark(
                flags,
                "RUN_DATA_GENERATOR",
                "RUN_DATA_PLATFORM",
                "RUN_FEATURE_STORE",
                "RUN_MODEL_PIPELINE",
                "RUN_KFP",
                "RUN_DOCKER_BASE",
                "RUN_DOCKER_DATA_GENERATOR",
                "RUN_DOCKER_DATAFLOW",
                "RUN_DOCKER_FEATURE_STORE",
                "RUN_DOCKER_TRAINING",
            )

        if path.startswith("configs/"):
            mark(flags, "RUN_DATA_GENERATOR", "RUN_DATA_PLATFORM", "RUN_FEATURE_STORE", "RUN_MODEL_PIPELINE")

        if path.startswith("apps/api/"):
            mark(flags, "RUN_API")

        if path.startswith("apps/data-platform/data-generator/") or path.startswith("tests/unit/data_generator/"):
            mark(flags, "RUN_DATA_GENERATOR")
            if path == "apps/data-platform/data-generator/Dockerfile":
                mark(flags, "RUN_DOCKER_DATA_GENERATOR")

        if (
            path.startswith("apps/data-platform/src/")
            or path == "apps/data-platform/pyproject.toml"
            or path.startswith("apps/data-platform/Dockerfile.")
            or path in {
                "tests/unit/data_platform/test_data_platform.py",
                "tests/contract/test_docker_dataflow_contracts.py",
            }
        ):
            mark(flags, "RUN_DATA_PLATFORM")
            if parts[-1].startswith("Dockerfile."):
                mark(flags, "RUN_DOCKER_DATAFLOW")

        if path.startswith("apps/data-platform/feature-store/") or path.startswith("tests/unit/feature_store/") or path.startswith(
            "apps/data-platform/src/feature_store/"
        ):
            mark(flags, "RUN_FEATURE_STORE")
            if path == "apps/data-platform/feature-store/Dockerfile":
                mark(flags, "RUN_DOCKER_FEATURE_STORE")

        if path.startswith("apps/ml-system/") or path.startswith("tests/unit/ml_system/"):
            mark(flags, "RUN_MODEL_PIPELINE")
            if path == "apps/ml-system/Dockerfile.training":
                mark(flags, "RUN_DOCKER_BASE", "RUN_DOCKER_TRAINING")

        if path.startswith("infra/kubeflow/") or path == "tests/unit/ml_system/test_kubeflow_pipeline_utils.py":
            mark(flags, "RUN_MODEL_PIPELINE", "RUN_KFP")

        if path.startswith("infra/helm/"):
            mark(flags, "RUN_HELM")

        if path.startswith("infra/docker/"):
            mark(flags, "RUN_DATA_PLATFORM", "RUN_DOCKER_DATAFLOW")
            if parts[-1] in {"Dockerfile.base-python", "Dockerfile.training"}:
                mark(flags, "RUN_DOCKER_BASE", "RUN_DOCKER_TRAINING", "RUN_MODEL_PIPELINE")

    if flags["RUN_DATA_GENERATOR"] or flags["RUN_DATA_PLATFORM"] or flags["RUN_FEATURE_STORE"] or flags["RUN_MODEL_PIPELINE"]:
        flags["RUN_PYTHON"] = True
    return flags


def main() -> int:
    parser = argparse.ArgumentParser(description="Map changed paths to Jenkins component flags.")
    parser.add_argument("--base-ref", default="")
    args = parser.parse_args()

    paths = changed_paths(args.base_ref or None)
    flags = classify(paths)
    components = [name.removeprefix("RUN_").lower() for name, enabled in flags.items() if enabled]

    for name in sorted(flags):
        print(f"{name}={'true' if flags[name] else 'false'}")
    print(f"CHANGED_COMPONENTS={','.join(components) if components else 'docs-only'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
