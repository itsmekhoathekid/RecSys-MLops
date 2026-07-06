from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_pipeline_package(
    package_path: str | Path,
    required_images: list[str],
    forbidden_tokens: list[str],
) -> dict[str, object]:
    path = Path(package_path)
    if not path.exists():
        raise FileNotFoundError(f"Kubeflow pipeline package not found: {path}")

    compiled = path.read_text(encoding="utf-8")
    failures: list[str] = []

    for token in forbidden_tokens:
        if token and token in compiled:
            failures.append(f"forbidden token found in compiled package: {token}")

    for image in required_images:
        if image and image not in compiled:
            failures.append(f"required image missing from compiled package: {image}")

    result: dict[str, object] = {
        "package_path": str(path),
        "required_images": required_images,
        "forbidden_tokens": forbidden_tokens,
        "valid": not failures,
    }
    if failures:
        result["failures"] = failures
        raise ValueError(json.dumps(result, indent=2, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a compiled Kubeflow pipeline package image contract.")
    parser.add_argument("--package-path", default="infra/kubeflow/compiled/bst_training_pipeline.yaml")
    parser.add_argument("--required-image", action="append", default=[])
    parser.add_argument("--forbidden-token", action="append", default=[])
    args = parser.parse_args()
    forbidden_tokens = args.forbidden_token or [":local"]

    result = validate_pipeline_package(
        package_path=args.package_path,
        required_images=args.required_image,
        forbidden_tokens=forbidden_tokens,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
