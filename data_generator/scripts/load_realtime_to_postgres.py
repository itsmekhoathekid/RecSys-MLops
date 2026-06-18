from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from data_generator.config import load_config
from data_generator.pipeline import HistoricalDataPipeline
from data_generator.sinks.postgres_sink import DEFAULT_TABLE_ORDER, load_run_to_postgres


def conninfo() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'recsys')} "
        f"user={os.getenv('POSTGRES_USER', 'recsys')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'recsys')}"
    )


def main() -> int:
    import psycopg

    parser = argparse.ArgumentParser(description="Load a deterministic realtime source sample into Postgres.")
    parser.add_argument("--config", default="config/data_generator_test.yaml")
    parser.add_argument("--limit-per-table", type=int, default=int(os.getenv("REALTIME_LIMIT_PER_TABLE", "200")))
    parser.add_argument("--tables", nargs="*", default=DEFAULT_TABLE_ORDER)
    args = parser.parse_args()

    config = load_config(args.config)
    result = HistoricalDataPipeline(config).run()
    run_path = Path(result["run_path"])
    with psycopg.connect(conninfo()) as connection:
        counts = load_run_to_postgres(
            run_path,
            connection,
            tables=args.tables,
            limit_per_table=args.limit_per_table,
        )
    print(json.dumps({"run_path": str(run_path), "loaded": counts}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
