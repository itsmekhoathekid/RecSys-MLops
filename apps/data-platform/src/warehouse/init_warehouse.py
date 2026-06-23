from __future__ import annotations

from .connection import connect
from .writer import ensure_warehouse


def main() -> int:
    with connect() as connection:
        ensure_warehouse(connection)
    print("Warehouse schemas initialized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

