from __future__ import annotations

import argparse
import json
import os
import time

from ingest.bronze_cdc_reader import read_bronze_cdc_table


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Kafka CDC bronze files in MinIO.")
    parser.add_argument("--topic", default="cdc.behavior_events")
    parser.add_argument(
        "--bronze-root",
        default=f"s3://{os.getenv('LAKE_BUCKET', 'recsys-lake')}/bronze/kafka",
    )
    parser.add_argument("--min-records", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout_seconds
    count = 0
    while time.monotonic() <= deadline:
        frame = read_bronze_cdc_table(args.bronze_root, args.topic)
        count = len(frame)
        if count >= args.min_records:
            break
        time.sleep(args.poll_seconds)
    else:
        raise SystemExit(
            f"Expected at least {args.min_records} bronze CDC records for {args.topic}, found {count}"
        )
    print(json.dumps({"topic": args.topic, "records": count}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
