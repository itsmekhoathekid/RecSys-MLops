from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from config import load_config
from offline.historical_pipeline import HistoricalDataPipeline
from validation import validate_drift_output, validate_parquet_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recsys synthetic data generator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate historical data")
    generate.add_argument("--config", required=True, help="Path to YAML config")

    validate = subparsers.add_parser("validate", help="Validate generated parquet")
    validate.add_argument("--config", required=True, help="Path to YAML config")
    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    config = load_config(args.config)

    if args.command == "generate":
        result = HistoricalDataPipeline(config).run()
        print(json.dumps(result["manifest"]["row_counts"], indent=2))
        print(f"Output: {result['run_path']}")
        return 0

    run_path = Path(config.output.base_path) / config.output.run_id
    result = validate_parquet_output(run_path, config)
    drift_result = validate_drift_output(run_path, config)
    errors = result.errors + drift_result.errors
    print(
        json.dumps(
            {
                "passed": not errors,
                **result.metrics,
                **drift_result.metrics,
                "errors": errors,
            },
            indent=2,
        )
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
