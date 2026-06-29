from __future__ import annotations

import subprocess
from pathlib import Path


def apply_feature_repo(repo_path: str | Path) -> None:
    subprocess.run(["feast", "apply"], cwd=Path(repo_path), check=True)
