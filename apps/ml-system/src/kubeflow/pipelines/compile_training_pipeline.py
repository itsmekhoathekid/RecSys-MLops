from __future__ import annotations

import argparse
import sys
import importlib
from pathlib import Path

from kfp import compiler


REPO_ROOT = Path(__file__).resolve().parents[5]
SOURCE_ROOTS = [
    REPO_ROOT / "apps/ml-system/src",
    REPO_ROOT / "apps/data-platform/src",
    REPO_ROOT,
]
for source_root in reversed(SOURCE_ROOTS):
    source_path = str(source_root)
    if source_path not in sys.path:
        sys.path.insert(0, source_path)

DEFAULT_PACKAGE_PATH = REPO_ROOT / "infra/kubeflow/compiled/bst_training_pipeline.yaml"


def compile_pipeline(package_path: str | Path = DEFAULT_PACKAGE_PATH) -> Path:
    output_path = Path(package_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pipeline_module = importlib.import_module("kubeflow.pipelines.bst_training_pipeline")
    pipeline_module = importlib.reload(pipeline_module)
    compiler.Compiler().compile(
        pipeline_func=pipeline_module.recsys_bst_pipeline,
        package_path=str(output_path),
    )
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Compile the RecSys BST Kubeflow pipeline package.")
    parser.add_argument("--package-path", default=str(DEFAULT_PACKAGE_PATH))
    args = parser.parse_args()

    print(compile_pipeline(args.package_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
