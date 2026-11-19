"""Apply `migrations/postgres/*.sql` to the reconciler's Postgres, in order, once each.

The Postgres twin of `mysql_migrate.py`, and smaller because Postgres DDL is transactional:
each file and its `schema_migrations` row commit together, so a file is applied or it is not.

    DATABASE_URL=postgres://... python scripts/pg_migrate.py [--dry-run]

A file that was applied and then edited is refused rather than re-run: the fix is a new file.
"""

import argparse
import hashlib
import os
import pathlib

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations" / "postgres"

_BOOKKEEPING = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        filename    text        PRIMARY KEY,
        checksum    text        NOT NULL,
        applied_at  timestamptz NOT NULL DEFAULT now()
    )
"""


def checksum(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pending(applied: dict[str, str], files: list[pathlib.Path]) -> list[pathlib.Path]:
    """The files not yet applied; raises if an applied one has changed since."""
    todo = []
    for path in files:
        if path.name not in applied:
            todo.append(path)
        elif applied[path.name] != checksum(path):
            raise SystemExit(f"{path.name} was applied and has since been edited; "
                             f"add a new migration instead of changing an applied one")
    return todo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="say what would be applied, connect but change nothing")
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")
    files = sorted(MIGRATIONS.glob("*.sql"))
    if not files:
        raise SystemExit(f"no migrations found in {MIGRATIONS}")

    import psycopg

    with psycopg.connect(dsn) as connection:
        connection.execute(_BOOKKEEPING)
        connection.commit()
        applied = dict(connection.execute(
            "SELECT filename, checksum FROM schema_migrations").fetchall())
        todo = pending(applied, files)
        if not todo:
            print(f"up to date — {len(files)} migration(s) already applied")
            return 0

        print(f"pending: {', '.join(p.name for p in todo)}")
        if args.dry_run:
            print("[dry-run] nothing applied")
            return 0

        for path in todo:
            # No parameters, so psycopg sends the file as one simple query and Postgres runs
            # every statement in it.
            connection.execute(path.read_text())
            connection.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                (path.name, checksum(path)))
            connection.commit()
            print(f"applied {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
