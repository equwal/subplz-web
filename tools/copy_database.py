"""Copy every row of the app database to another database.

The web server needs this once, when it turns on burst workers: the workers
share a Postgres database with it, and until then the data is in SQLite (see
infra/burst/README.md).

    python -m tools.copy_database sqlite:////opt/subplz-web/data/subplz.db \\
        "postgresql+psycopg://user:password@host:port/db?sslmode=require"

The target gets the tables of the app models first. The copy refuses a target
that has rows already, so it never mixes two databases.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine, func, insert, select

from backend.db import Base

BATCH = 1000


def _as_utc(table, row: dict) -> dict:
    """SQLite keeps no time zone. The app writes UTC, so say so for Postgres."""
    for column in table.columns:
        value = row.get(column.name)
        if (isinstance(column.type, DateTime) and column.type.timezone
                and isinstance(value, datetime) and value.tzinfo is None):
            row[column.name] = value.replace(tzinfo=timezone.utc)
    return row


def copy_database(source_url: str, target_url: str) -> dict[str, int]:
    """Copy each table of the app from `source_url` to `target_url`.

    Returns the number of rows copied for each table.
    """
    source = create_engine(source_url)
    target = create_engine(target_url)
    Base.metadata.create_all(target)
    counts: dict[str, int] = {}
    with source.connect() as src, target.begin() as dst:
        for table in Base.metadata.sorted_tables:
            if dst.execute(select(func.count()).select_from(table)).scalar():
                raise RuntimeError(f"table {table.name} has rows in the target already")
        # sorted_tables puts each table after the tables that it refers to.
        for table in Base.metadata.sorted_tables:
            rows = [_as_utc(table, dict(r._mapping)) for r in src.execute(select(table))]
            for start in range(0, len(rows), BATCH):
                dst.execute(insert(table), rows[start:start + BATCH])
            counts[table.name] = len(rows)
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    for table, count in copy_database(argv[0], argv[1]).items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
