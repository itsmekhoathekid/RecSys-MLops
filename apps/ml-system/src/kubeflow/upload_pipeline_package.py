from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def _attr_or_key(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _first_field(obj: Any, fields: tuple[str, ...]) -> str:
    for field in fields:
        value = _attr_or_key(obj, field)
        if value:
            return str(value)
    return ""


def upload_or_version_pipeline(
    client: Any,
    package_path: str,
    pipeline_name: str,
    version_name: str,
    description: str,
) -> dict[str, str]:
    pipeline_id = client.get_pipeline_id(pipeline_name)
    if pipeline_id:
        version = client.upload_pipeline_version(
            pipeline_package_path=package_path,
            pipeline_version_name=version_name,
            pipeline_id=pipeline_id,
            description=description,
        )
        return {
            "action": "uploaded_pipeline_version",
            "pipeline_id": pipeline_id,
            "pipeline_name": pipeline_name,
            "pipeline_version_id": _first_field(version, ("pipeline_version_id", "pipeline_versionid", "id")),
            "pipeline_version_name": version_name,
        }

    pipeline = client.upload_pipeline(
        pipeline_package_path=package_path,
        pipeline_name=pipeline_name,
        description=description,
    )
    return {
        "action": "uploaded_pipeline",
        "pipeline_id": _first_field(pipeline, ("pipeline_id", "pipelineid", "id")),
        "pipeline_name": pipeline_name,
        "pipeline_version_name": version_name,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a compiled Kubeflow pipeline package without creating a run.")
    parser.add_argument("--host", default=os.getenv("KFP_ENDPOINT", "http://127.0.0.1:8888"))
    parser.add_argument("--package-path", default="infra/kubeflow/compiled/bst_training_pipeline.yaml")
    parser.add_argument("--pipeline-name", default=os.getenv("KFP_PIPELINE_NAME", "recsys-bst-feature-train-evaluate"))
    parser.add_argument("--pipeline-version-name", default=os.getenv("KFP_PIPELINE_VERSION_NAME", ""))
    parser.add_argument("--description", default="RecSys BST training pipeline package uploaded by Jenkins CI/CD.")
    args = parser.parse_args()

    package_path = Path(args.package_path)
    if not package_path.exists():
        raise FileNotFoundError(f"Kubeflow pipeline package not found: {package_path}")

    version_name = args.pipeline_version_name.strip()
    if not version_name:
        image_tag = os.getenv("IMAGE_TAG") or os.getenv("GIT_COMMIT") or str(int(time.time()))
        build_number = os.getenv("BUILD_NUMBER") or str(int(time.time()))
        version_name = f"ci-{image_tag[:12]}-build-{build_number}"

    import kfp

    client = kfp.Client(host=args.host)
    result = upload_or_version_pipeline(
        client=client,
        package_path=str(package_path),
        pipeline_name=args.pipeline_name,
        version_name=version_name,
        description=args.description,
    )
    result.update({"host": args.host, "package_path": str(package_path)})
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
