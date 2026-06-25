from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from feature_store.feast_registry import apply_and_materialize_incremental
from feature_store.offline_to_online_sync import sync_offline_to_online


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-ts", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--repo-path", default=os.getenv("FEAST_REPO_PATH", "apps/data-platform/feature-store/feature_repo"))
    parser.add_argument("--registry-backup-uri", default=os.getenv("FEAST_REGISTRY_BACKUP_URI"))
    parser.add_argument("--offline-root", default=os.getenv("FEAST_OFFLINE_ROOT", "s3://recsys-feature-store/offline"))
    parser.add_argument("--run-id", default=os.getenv("ONLINE_STORE_SYNC_RUN_ID", datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")))
    args = parser.parse_args()
    result = apply_and_materialize_incremental(
        args.end_ts,
        repo_path=args.repo_path,
        backup_uri=args.registry_backup_uri,
    )
    import redis

    client = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        decode_responses=True,
    )
    online_sync = sync_offline_to_online(args.offline_root, client, run_id=args.run_id)
    print(json.dumps({"end_ts": args.end_ts, "run_id": args.run_id, "online_sync": online_sync, **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
