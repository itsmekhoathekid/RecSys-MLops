from __future__ import annotations

from typing import Any

import pandas as pd


def read_table(connection: Any, qualified_name: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {qualified_name}", connection)


def read_production_tables(connection: Any) -> dict[str, pd.DataFrame]:
    return {
        "fact_behavior_events": read_table(connection, "production.fact_behavior_events"),
        "fact_impressions": read_table(connection, "production.fact_impressions"),
        "fact_orders": read_table(connection, "production.fact_orders"),
        "dim_products_scd": read_table(connection, "production.dim_products_scd"),
    }

