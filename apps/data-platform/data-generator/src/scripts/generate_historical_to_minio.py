from __future__ import annotations

import argparse
import os
from pathlib import Path

from config import load_config
from offline.historical_pipeline import HistoricalDataPipeline
from sinks.minio_sink import copy_run_to_minio_layout, upload_run_to_minio


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/local/data_generator_test.yaml")
    parser.add_argument("--lake-root", default="data_platform/lake")
    parser.add_argument("--target", choices=["local", "s3"], default=os.getenv("GENERATOR_TARGET", "local"))
    parser.add_argument("--bucket", default=os.getenv("LAKE_BUCKET", "recsys-lakehouse"))
    parser.add_argument("--prefix", default="raw")
    args = parser.parse_args()
    config = load_config(args.config)
    result = HistoricalDataPipeline(config).run()
    if args.target == "s3":
        uploaded = upload_run_to_minio(
            result["run_path"],
            bucket=args.bucket,
            prefix=args.prefix,
            run_id=config.output.run_id,
            endpoint_url=os.getenv("MINIO_ENDPOINT", "http://minio:9000"),
            access_key=os.getenv("MINIO_ROOT_USER", "minio"),
            secret_key=os.getenv("MINIO_ROOT_PASSWORD", "minio123"),
        )
        print({"uploaded_count": len(uploaded), "bucket": args.bucket, "prefix": args.prefix})
    else:
        destination = copy_run_to_minio_layout(
            result["run_path"], Path(args.lake_root), config.output.run_id
        )
        print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
