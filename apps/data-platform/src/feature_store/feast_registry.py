from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _repo_import_context(repo_root: Path):
    previous_cwd = Path.cwd()
    repo_root = repo_root.resolve()
    repo_root_value = str(repo_root)
    added_path = repo_root_value not in sys.path
    if added_path:
        sys.path.insert(0, repo_root_value)
    os.chdir(repo_root)
    try:
        yield
    finally:
        os.chdir(previous_cwd)
        if added_path:
            try:
                sys.path.remove(repo_root_value)
            except ValueError:
                pass


def apply_feature_repo(repo_path: str | Path, skip_source_validation: bool = False) -> None:
    from feast import FeatureStore
    from feast.repo_operations import _get_repo_contents, apply_total_with_repo_instance

    repo_root = Path(repo_path).resolve()
    store = FeatureStore(repo_path=str(repo_root))
    with _repo_import_context(repo_root):
        repo = _get_repo_contents(repo_root, project_name=store.project, repo_config=store.config)
        apply_total_with_repo_instance(
            store,
            store.project,
            store.registry,
            repo,
            skip_source_validation=skip_source_validation,
            skip_feature_view_validation=True,
        )
