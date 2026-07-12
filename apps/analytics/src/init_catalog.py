from __future__ import annotations

import json

from sync_silver import AnalyticsSyncConfig, build_spark


def initialize_catalog(config: AnalyticsSyncConfig | None = None) -> str:
    config = config or AnalyticsSyncConfig.from_env()
    spark = build_spark(config)
    try:
        namespace = f"{config.target_catalog}.{config.target_namespace}"
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        return namespace
    finally:
        spark.stop()


if __name__ == "__main__":
    print(json.dumps({"status": "ok", "namespace": initialize_catalog()}))

