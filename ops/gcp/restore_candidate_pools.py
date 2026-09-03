from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

from psycopg import sql
from redis import Redis

from feature_store.postgres_offline_store import PostgresOfflineStoreConfig
from features.flink.features.candidate_pool import candidate_updates


MAX_CANDIDATES_PER_POOL = 100
AGGREGATE_PATTERNS = ("candidate:popular:*", "candidate:trending:*")


def latest_active_items(
    config: PostgresOfflineStoreConfig,
) -> list[dict[str, Any]]:
    query = sql.SQL(
        """
        WITH ranked AS (
          SELECT product_id, category_id, views_1h, carts_1h, purchases_24h,
                 popularity_score, is_active,
                 ROW_NUMBER() OVER (
                   PARTITION BY product_id
                   ORDER BY feature_timestamp DESC NULLS LAST,
                            created_timestamp DESC NULLS LAST
                 ) AS recency_rank
          FROM {}.item_features
        )
        SELECT product_id, category_id, views_1h, carts_1h, purchases_24h,
               popularity_score
        FROM ranked
        WHERE recency_rank = 1 AND COALESCE(is_active, TRUE)
        ORDER BY product_id
        """
    ).format(sql.Identifier(config.schema))
    with config.connect() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            columns = [column.name for column in cursor.description]
            return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def candidate_pools(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    pools: dict[str, dict[str, float]] = {}
    for row in rows:
        for key, product_id, score in candidate_updates(row):
            pools.setdefault(key, {})[str(product_id)] = float(score)
    return pools


def replace_aggregate_candidate_pools(
    redis_client: Any,
    pools: dict[str, dict[str, float]],
) -> dict[str, int]:
    stale = {
        key.decode("utf-8") if isinstance(key, bytes) else str(key)
        for pattern in AGGREGATE_PATTERNS
        for key in redis_client.scan_iter(match=pattern)
    }
    pipeline = redis_client.pipeline(transaction=True)
    if stale:
        pipeline.delete(*sorted(stale))
    for key, scored_items in sorted(pools.items()):
        if not scored_items:
            continue
        pipeline.zadd(key, scored_items)
        pipeline.zremrangebyrank(key, 0, -MAX_CANDIDATES_PER_POOL - 1)
    pipeline.execute()
    return {
        "items": len(pools.get("candidate:popular:global", {})),
        "pools": len(pools),
        "stale_pools_replaced": len(stale),
    }


def main() -> int:
    config = PostgresOfflineStoreConfig.from_env()
    client = Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    rows = latest_active_items(config)
    if not rows:
        raise RuntimeError("Feature PostgreSQL has no active item snapshot")
    result = replace_aggregate_candidate_pools(client, candidate_pools(rows))
    if result["items"] == 0:
        raise RuntimeError("candidate:popular:global was not restored")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
