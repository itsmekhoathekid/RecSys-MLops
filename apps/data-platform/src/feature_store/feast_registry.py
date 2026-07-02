from __future__ import annotations

from pathlib import Path


def apply_feature_repo(repo_path: str | Path, skip_source_validation: bool = False) -> None:
    from feast import FeatureStore
    from feast.repo_operations import _get_repo_contents, apply_total_with_repo_instance

    repo_root = Path(repo_path)
    store = FeatureStore(repo_path=str(repo_root))
    repo = _get_repo_contents(repo_root, project_name=store.project, repo_config=store.config)
    apply_total_with_repo_instance(
        store,
        store.project,
        store.registry,
        repo,
        skip_source_validation=skip_source_validation,
        skip_feature_view_validation=True,
    )
