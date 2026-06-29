from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from local.run_batch_features import run_batch_features


def build_runtime_config(
    source_config_path: str,
    output_base: str,
    run_path: str | None,
    runtime_config_path: str,
) -> str:
    with Path(source_config_path).open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    base = Path(output_base)
    config["output"]["base_path"] = str(base)
    config["output"]["silver_path"] = str(base / "silver")
    config["output"]["offline_feature_path"] = str(base / "feature_store" / "offline")
    config["output"]["ml_artifact_path"] = str(base / "ml" / "offline")
    if run_path:
        config["input"]["run_path"] = run_path

    target = Path(runtime_config_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return str(target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run feature engineering with runtime output paths")
    parser.add_argument("--source-config", default="configs/local/spark_batch.yaml")
    parser.add_argument("--output-base", default="/workspace/recsys/data_platform/output")
    parser.add_argument("--run-path", default="")
    parser.add_argument("--runtime-config", default="/tmp/recsys_spark_batch.yaml")
    parser.add_argument("--summary-path", default="")
    args = parser.parse_args()

    runtime_config = build_runtime_config(
        source_config_path=args.source_config,
        output_base=args.output_base,
        run_path=args.run_path or None,
        runtime_config_path=args.runtime_config,
    )
    summary = run_batch_features(runtime_config)
    if args.summary_path:
        Path(args.summary_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary_path).write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

