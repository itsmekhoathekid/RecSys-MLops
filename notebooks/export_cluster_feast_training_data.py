from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

import boto3
import pandas as pd
from feast import Entity, FeatureStore, FeatureView, Field, FileSource, ValueType
from feast.types import Float64, Int64, String


FEATURE_COLUMNS = [
    "views_30m",
    "carts_30m",
    "purchases_24h",
    "distinct_categories_7d",
    "avg_viewed_price_7d",
    "cart_to_purchase_ratio_7d",
    "last_event_age_seconds",
]


def download_prefix(
    *,
    bucket: str,
    prefix: str,
    destination: Path,
    endpoint_url: str,
    access_key: str,
    secret_key: str,
) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
    )
    paginator = client.get_paginator("list_objects_v2")
    downloaded = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            relative = key[len(prefix.rstrip("/") + "/") :]
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
            downloaded += 1
    return downloaded


def build_cluster_feast_repo(repo_path: Path, offline_path: Path) -> FeatureStore:
    if repo_path.exists():
        shutil.rmtree(repo_path)
    repo_path.mkdir(parents=True)
    (repo_path / "feature_store.yaml").write_text(
        "\n".join(
            [
                "project: recsys_cluster_ml",
                "provider: local",
                "registry: registry.db",
                "offline_store:",
                "  type: file",
                "entity_key_serialization_version: 3",
                "",
            ]
        ),
        encoding="utf-8",
    )

    user = Entity(name="user", join_keys=["user_id"], value_type=ValueType.INT64)
    source = FileSource(
        name="cluster_user_aggregate_features_source",
        path=str(offline_path),
        timestamp_field="feature_timestamp",
    )
    feature_view = FeatureView(
        name="cluster_user_aggregate_features",
        entities=[user],
        ttl=None,
        schema=[
            Field(name="views_30m", dtype=Int64),
            Field(name="carts_30m", dtype=Int64),
            Field(name="purchases_24h", dtype=Int64),
            Field(name="distinct_categories_7d", dtype=Int64),
            Field(name="avg_viewed_price_7d", dtype=Float64),
            Field(name="cart_to_purchase_ratio_7d", dtype=Float64),
            Field(name="last_event_age_seconds", dtype=Int64),
            Field(name="feature_version", dtype=String),
        ],
        source=source,
        online=False,
    )

    store = FeatureStore(repo_path=str(repo_path))
    store.apply([user, feature_view])
    return store


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a training dataset by running Feast inside a K8s pod against cluster offline data."
    )
    parser.add_argument("--labels", required=True, help="Parquet file with generated user_id,label rows")
    parser.add_argument("--output", required=True, help="Output parquet path for merged training data")
    parser.add_argument("--bucket", default=os.getenv("FEAST_CLUSTER_BUCKET", "recsys-feature-store"))
    parser.add_argument(
        "--prefix",
        default=os.getenv("FEAST_CLUSTER_PREFIX", "offline/user_aggregate_features"),
    )
    parser.add_argument(
        "--endpoint-url",
        default=os.getenv("MINIO_ENDPOINT", "http://data-platform-minio:9000"),
    )
    parser.add_argument("--access-key", default=os.getenv("AWS_ACCESS_KEY_ID", "minio"))
    parser.add_argument("--secret-key", default=os.getenv("AWS_SECRET_ACCESS_KEY", "minio123"))
    parser.add_argument("--work-dir", default="/tmp/recsys_cluster_feast_export")
    args = parser.parse_args()

    work_dir = Path(args.work_dir)
    offline_path = work_dir / "offline" / "user_aggregate_features"
    repo_path = work_dir / "feature_repo"
    output_path = Path(args.output)
    labels_path = Path(args.labels)

    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    downloaded = download_prefix(
        bucket=args.bucket,
        prefix=args.prefix,
        destination=offline_path,
        endpoint_url=args.endpoint_url,
        access_key=args.access_key,
        secret_key=args.secret_key,
    )
    if downloaded == 0:
        raise FileNotFoundError(f"No parquet files found at s3://{args.bucket}/{args.prefix}")

    cluster_source = pd.read_parquet(offline_path)
    labels = pd.read_parquet(labels_path)[["user_id", "label"]].drop_duplicates("user_id")
    entity_df = cluster_source[["user_id", "feature_timestamp"]].rename(
        columns={"feature_timestamp": "event_timestamp"}
    )
    entity_df = entity_df.sort_values(["user_id", "event_timestamp"]).reset_index(drop=True)

    store = build_cluster_feast_repo(repo_path=repo_path, offline_path=offline_path)
    feast_df = store.get_historical_features(
        entity_df=entity_df,
        features=[f"cluster_user_aggregate_features:{column}" for column in FEATURE_COLUMNS],
    ).to_df()
    training_df = feast_df.merge(labels, on="user_id", how="inner")
    training_df[FEATURE_COLUMNS] = training_df[FEATURE_COLUMNS].fillna(0)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    training_df.to_parquet(output_path, index=False)

    print(f"cluster_bucket=s3://{args.bucket}/{args.prefix}")
    print(f"downloaded_cluster_parquet_files={downloaded}")
    print(f"cluster_offline_source_rows={len(cluster_source)}")
    print(f"cluster_offline_unique_users={cluster_source['user_id'].nunique()}")
    print(f"generated_label_rows={len(labels)}")
    print(f"feast_entity_rows={len(entity_df)}")
    print(f"feast_historical_rows={len(feast_df)}")
    print(f"merged_training_rows={len(training_df)}")
    print(f"label_distribution={training_df['label'].value_counts().sort_index().to_dict()}")
    print(
        "event_timestamp_range="
        f"{entity_df['event_timestamp'].min()} -> {entity_df['event_timestamp'].max()}"
    )
    print(f"output={output_path}")
    print(training_df[["user_id", "label", *FEATURE_COLUMNS]].head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
