from __future__ import annotations

import os
from decimal import Decimal

import pyarrow as pa

from schemas import SCHEMAS


def arrow_type_to_sql(data_type: pa.DataType) -> str:
    if pa.types.is_int16(data_type):
        return "SMALLINT"
    if pa.types.is_int32(data_type):
        return "INTEGER"
    if pa.types.is_int64(data_type):
        return "BIGINT"
    if pa.types.is_float64(data_type):
        return "DOUBLE PRECISION"
    if pa.types.is_boolean(data_type):
        return "BOOLEAN"
    if pa.types.is_timestamp(data_type):
        return "TIMESTAMPTZ"
    if pa.types.is_date32(data_type):
        return "DATE"
    if pa.types.is_decimal(data_type):
        return f"DECIMAL({data_type.precision}, {data_type.scale})"
    return "TEXT"


PRIMARY_KEYS = {
    "users": ["user_id"],
    "user_preferences": ["user_id", "category_id", "brand_id"],
    "products": ["product_id"],
    "product_snapshots": ["product_id", "valid_from"],
    "sessions": ["session_id"],
    "recommendation_requests": ["request_id"],
    "impressions": ["impression_id"],
    "behavior_events": ["event_id", "payload_hash"],
    "orders": ["order_id"],
    "order_items": ["order_item_id"],
}


def build_table_ddl(table_name: str, schema: pa.Schema) -> str:
    columns = [
        f"  {field.name} {arrow_type_to_sql(field.type)}"
        for field in schema
    ]
    pk = PRIMARY_KEYS.get(table_name)
    if pk:
        columns.append(f"  PRIMARY KEY ({', '.join(pk)})")
    body = ",\n".join(columns)
    return f"CREATE TABLE IF NOT EXISTS {table_name} (\n{body}\n);"


def build_all_ddl() -> str:
    return "\n\n".join(
        build_table_ddl(table_name, schema)
        for table_name, schema in SCHEMAS.items()
    )


def main() -> int:
    import psycopg

    conninfo = (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'recsys')} "
        f"user={os.getenv('POSTGRES_USER', 'recsys')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'recsys')}"
    )
    with psycopg.connect(conninfo) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('recsys_source_schema_init'))")
            cursor.execute(build_all_ddl())
        connection.commit()
    print("Postgres source schema initialized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
