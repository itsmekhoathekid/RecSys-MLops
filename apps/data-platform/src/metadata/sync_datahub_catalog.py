from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict

from metadata.datahub_client import DataHubCatalogClient
from metadata.governance_catalog import catalog_products, validate_catalog
from monitoring.pushgateway import MetricSample, push_metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync the static RecSys batch dataset catalog to DataHub."
    )
    parser.add_argument(
        "--gms-url",
        default=os.getenv("DATAHUB_GMS_URL", "http://localhost:8088"),
    )
    parser.add_argument("--pushgateway-url", default=os.getenv("PUSHGATEWAY_URL", ""))
    parser.add_argument(
        "--strict",
        action="store_true",
        default=os.getenv("DATAHUB_INGEST_STRICT", "").lower() in {"1", "true", "yes"},
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify that the remote DataHub catalog matches the local static catalog.",
    )
    parser.add_argument(
        "--require-results",
        action="store_true",
        help="Require a completed result for every managed CUSTOM assertion.",
    )
    return parser.parse_args()


def sync_catalog(client: DataHubCatalogClient, products) -> dict[str, object]:
    synced = client.sync(products)
    # Data Product lookup is backed by DataHub's search index, which can lag the
    # successful GraphQL mutation by a few seconds. Keep the catalog operation
    # strict, but tolerate that bounded propagation delay before failing CI/CD.
    for attempt in range(6):
        try:
            remote = client.verify_remote(products)
            break
        except RuntimeError:
            if attempt == 5:
                raise
            time.sleep(min(2**attempt, 8))
    return {
        "mode": "dataset-only-static",
        **asdict(synced),
        "assertions_with_results": remote.assertions_with_results,
        "verified": remote.verified,
    }


def metric_samples(summary: dict[str, object]) -> list[MetricSample]:
    success = 1.0 if summary.get("verified") else 0.0
    samples = [
        MetricSample("recsys_datahub_ingest_success", success),
        MetricSample(
            "recsys_datahub_ingest_timestamp_seconds", float(int(time.time()))
        ),
        MetricSample(
            "recsys_datahub_ingest_dataset_count", float(summary.get("datasets", 0))
        ),
        MetricSample(
            "recsys_datahub_ingest_lineage_edge_count",
            float(summary.get("lineage_edges", 0)),
        ),
        MetricSample(
            "recsys_datahub_ingest_data_product_count",
            float(summary.get("data_products", 0)),
        ),
        MetricSample(
            "recsys_datahub_ingest_assertion_count",
            float(summary.get("assertions", 0)),
        ),
        MetricSample(
            "recsys_datahub_ingest_contract_count",
            float(summary.get("data_contracts", 0)),
        ),
        MetricSample(
            "recsys_datahub_assertions_with_result_count",
            float(summary.get("assertions_with_results", 0)),
        ),
    ]
    for product in catalog_products():
        samples.append(
            MetricSample(
                "recsys_datahub_ingest_data_product_present",
                success,
                {"data_product": product.id},
            )
        )
    return samples


def push_sync_metrics(summary: dict[str, object], pushgateway_url: str | None) -> None:
    push_metrics(
        metric_samples(summary),
        "recsys_datahub_governance",
        gateway_url=pushgateway_url,
    )


def main() -> int:
    args = parse_args()
    products = catalog_products()
    client = DataHubCatalogClient.from_env(args.gms_url)
    try:
        if args.verify_only:
            coverage = validate_catalog(products)
            remote = client.verify_remote(
                products, require_results=getattr(args, "require_results", False)
            )
            summary = {**coverage, **asdict(remote)}
        else:
            if getattr(args, "require_results", False):
                raise ValueError(
                    "--require-results can only be used with --verify-only"
                )
            summary = sync_catalog(client, products)
    except Exception as exc:
        summary = {
            "mode": "dataset-only-static",
            "verified": False,
            "error": str(exc),
        }
        push_sync_metrics(summary, args.pushgateway_url or None)
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1 if args.strict or args.verify_only else 0
    finally:
        client.close()
    push_sync_metrics(summary, args.pushgateway_url or None)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
