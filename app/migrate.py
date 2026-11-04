"""Simple repeatable SQL migration runner (MVP-002).

Applies pending ``migrations/*.sql`` files in filename order and records
each applied file in a ``schema_migrations`` table, so re-running only
applies what is new. Individual migration files also use
``IF NOT EXISTS`` guards where practical.

Usage (from the repository root, with ``DATABASE_*`` configured)::

    python -m app.migrate

Only touches the configured database; never creates, drops, or alters
other databases.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.config import DatabaseConfig, get_database_config
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def pending_migrations(config: DatabaseConfig | None = None) -> list[str]:
    """Read-only inventory; never creates the ledger or applies SQL."""
    conn=get_connection() if config is None else get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT to_regclass('public.schema_migrations') AS name")
            row=cur.fetchone()
            name=row['name'] if isinstance(row,dict) else row[0]
            applied=set()
            if name:
                cur.execute('SELECT filename FROM schema_migrations')
                applied={r['filename'] if isinstance(r,dict) else r[0] for r in cur.fetchall()}
            return [p.name for p in _pending_migrations(applied)]
    finally:
        conn.rollback();conn.close()


def _pending_migrations(applied: set[str]) -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)
    return [p for p in files if p.name not in applied]


def run_migrations(config: DatabaseConfig | None = None) -> list[str]:
    """Apply pending migrations. Returns the filenames that were applied."""
    # Preserve the default operational caller; explicit configurations must
    # never fall back to DATABASE_* (the isolated demo owns a different role).
    if config is None:
        get_database_config()
    try:
        conn = get_connection() if config is None else get_connection(config)
    except DatabaseNotConfiguredError:
        raise
    except DatabaseUnavailableError:
        raise
    applied: set[str] = set()
    newly_applied: list[str] = []
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                cur.execute("SELECT filename FROM schema_migrations")
                applied = {row["filename"] if isinstance(row, dict) else row[0] for row in cur.fetchall()}
                for path in _pending_migrations(applied):
                    sql = path.read_text(encoding="utf-8")
                    if not sql.strip():
                        continue
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%(filename)s)",
                        {"filename": path.name},
                    )
                    newly_applied.append(path.name)
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("migration_failed") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return newly_applied


def main() -> int:
    if sys.argv[1:]==['--check']:
        import json
        try: pending=pending_migrations()
        except Exception:
            print('error: migration inventory unavailable',file=sys.stderr);return 1
        print(json.dumps({'pending':pending,'read_only':True}))
        return 3 if pending else 0
    if sys.argv[1:]:
        print('usage: python -m app.migrate [--check]',file=sys.stderr);return 2
    try:
        applied = run_migrations()
    except DatabaseNotConfiguredError:
        print("error: database_not_configured (set DATABASE_* env vars first)", file=sys.stderr)
        return 2
    except DatabaseUnavailableError as exc:
        print(f"error: database migration failed ({exc})", file=sys.stderr)
        return 1
    if applied:
        for name in applied:
            print(f"applied: {name}")
    else:
        print("up to date: no pending migrations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
