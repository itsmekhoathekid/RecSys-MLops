from __future__ import annotations

import argparse
from datetime import datetime, timezone

from pipelines.data_pipeline.feature_store.feast_registry import materialize_incremental


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-ts", default=datetime.now(timezone.utc).isoformat())
    args = parser.parse_args()
    result = materialize_incremental(args.end_ts)
    print(result.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

