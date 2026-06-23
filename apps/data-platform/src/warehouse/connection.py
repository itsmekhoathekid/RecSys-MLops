from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WarehouseConfig:
    host: str = "warehouse-postgres"
    port: int = 5432
    database: str = "recsys_warehouse"
    user: str = "recsys"
    password: str = "recsys"

    @classmethod
    def from_env(cls) -> "WarehouseConfig":
        return cls(
            host=os.getenv("WAREHOUSE_POSTGRES_HOST", os.getenv("WAREHOUSE_HOST", "warehouse-postgres")),
            port=int(os.getenv("WAREHOUSE_POSTGRES_PORT", os.getenv("WAREHOUSE_PORT", "5432"))),
            database=os.getenv("WAREHOUSE_POSTGRES_DB", os.getenv("WAREHOUSE_DB", "recsys_warehouse")),
            user=os.getenv("WAREHOUSE_POSTGRES_USER", os.getenv("WAREHOUSE_USER", "recsys")),
            password=os.getenv("WAREHOUSE_POSTGRES_PASSWORD", os.getenv("WAREHOUSE_PASSWORD", "recsys")),
        )

    @property
    def conninfo(self) -> str:
        return (
            f"host={self.host} port={self.port} dbname={self.database} "
            f"user={self.user} password={self.password}"
        )


def connect(config: WarehouseConfig | None = None):
    import psycopg

    return psycopg.connect((config or WarehouseConfig.from_env()).conninfo)

