from __future__ import annotations

import argparse
import json
from pathlib import Path

from feature_engineering.spark.spark_batch_entrypoint import run_pyspark_batch


def run_offline_features_from_warehouse(
    offline_root: str | Path | None = None,
    ml_root: str | Path | None = None,
    max_history_length: int = 50,
    label_window_hours: int = 24,
    config_path: str | Path = "configs/local/spark_batch.yaml",
) -> dict[str, int]:
    """Compatibility wrapper for the old warehouse export command.

    Feature generation is now owned by the PySpark batch entrypoint, which reads
    raw lake data and writes silver, offline feature-store, and ML artifacts.
    """
    del offline_root, ml_root, max_history_length, label_window_hours
    return run_pyspark_batch(config_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PySpark offline feature export.")
    parser.add_argument("--config", default="configs/local/spark_batch.yaml")
    parser.add_argument("--offline-root", default=None)
    parser.add_argument("--ml-root", default=None)
    parser.add_argument("--max-history-length", type=int, default=50)
    parser.add_argument("--label-window-hours", type=int, default=24)
    args = parser.parse_args()
    print(
        json.dumps(
            run_offline_features_from_warehouse(
                args.offline_root,
                args.ml_root,
                max_history_length=args.max_history_length,
                label_window_hours=args.label_window_hours,
                config_path=args.config,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
