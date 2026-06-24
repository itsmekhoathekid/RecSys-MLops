from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from feature_store.feast_registry import apply_and_materialize_incremental


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-ts", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--repo-path", default=os.getenv("FEAST_REPO_PATH", "apps/data-platform/feature-store/feature_repo"))
    parser.add_argument("--registry-backup-uri", default=os.getenv("FEAST_REGISTRY_BACKUP_URI"))
    args = parser.parse_args()
    result = apply_and_materialize_incremental(
        args.end_ts,
        repo_path=args.repo_path,
        backup_uri=args.registry_backup_uri,
    )
    print(json.dumps({"end_ts": args.end_ts, **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
