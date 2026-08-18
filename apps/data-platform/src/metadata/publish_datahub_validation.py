from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

from metadata.datahub_client import DataHubCatalogClient
from validate.report_io import read_validation_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish local dataset validation results to DataHub CUSTOM assertions."
    )
    parser.add_argument("--product", required=True)
    parser.add_argument("--report-uri", action="append", default=[])
    parser.add_argument("--expected-dataset-key", action="append", default=[])
    parser.add_argument(
        "--gms-url",
        default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8088"),
    )
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.expected_dataset_key:
        print(json.dumps({"published": False, "error": "No expected dataset keys"}))
        return 1
    reports = []
    read_errors = []
    for uri in args.report_uri:
        try:
            reports.append(read_validation_report(uri))
        except Exception as exc:
            read_errors.append({"uri": uri, "error": str(exc)})
    client = DataHubCatalogClient.from_env(args.gms_url)
    try:
        summary = client.publish_validation_reports(
            args.product,
            tuple(reports),
            tuple(args.expected_dataset_key),
        )
        output = {**asdict(summary), "read_errors": read_errors, "published": True}
    except Exception as exc:
        output = {"product": args.product, "published": False, "error": str(exc)}
        print(json.dumps(output, indent=2, sort_keys=True))
        return 1
    finally:
        client.close()
    print(json.dumps(output, indent=2, sort_keys=True))
    if args.strict and (summary.failure or summary.error):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
