from __future__ import annotations

import argparse
import json
from pathlib import Path

from feature_engineering.spark.spark_batch_entrypoint import run_pyspark_batch


def run_batch_features(config_path: str | Path = "configs/local/spark_batch.yaml") -> dict[str, int]:
    return run_pyspark_batch(config_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PySpark batch feature flow")
    parser.add_argument("--config", default="configs/local/spark_batch.yaml")
    args = parser.parse_args()
    print(json.dumps(run_batch_features(args.config), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
