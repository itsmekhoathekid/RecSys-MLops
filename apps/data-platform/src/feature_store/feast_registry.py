from __future__ import annotations

import subprocess
from pathlib import Path


def feast_repo_path(repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> Path:
    path = Path(repo_path)
    if not (path / "feature_store.yaml").exists():
        raise FileNotFoundError(f"Missing Feast feature_store.yaml in {path}")
    return path


def run_feast_command(args: list[str], repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> subprocess.CompletedProcess[str]:
    path = feast_repo_path(repo_path)
    return subprocess.run(
        ["feast", *args],
        cwd=path,
        check=True,
        text=True,
    )


def apply_feature_repo(repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> subprocess.CompletedProcess[str]:
    return run_feast_command(["apply"], repo_path)


def materialize_incremental(end_ts: str, repo_path: str | Path = "apps/data-platform/feature-store/feature_repo") -> subprocess.CompletedProcess[str]:
    return run_feast_command(["materialize-incremental", end_ts], repo_path)
