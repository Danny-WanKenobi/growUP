"""PostgreSQL connectivity checks for Grow Up.

Only used by the readiness endpoint. Connection failures are reported
as generic categories so responses and logs never leak credentials,
connection strings, or driver internals.
"""

from __future__ import annotations

from typing import Any

from app.config import DatabaseConfig, get_database_config


class DatabaseNotConfiguredError(RuntimeError):
    """Required DATABASE_* environment variables are missing/invalid."""


class DatabaseUnavailableError(RuntimeError):
    """A PostgreSQL connection attempt failed."""


def acquire_intake_locks(cur: Any, keys: list[str]) -> None:
    """Acquire transaction-scoped advisory locks in deterministic order.

    Keys are deduplicated and sorted so concurrent operations holding
    overlapping key sets always acquire in the same global order (no
    deadlocks). Locks are transaction-scoped: PostgreSQL releases them
    automatically at commit or rollback.

    Locks coordinate intake operations; they never prove physical
    identity. Callers must still run every identity check after
    acquiring them.
    """
    for key in sorted(set(keys)):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%(key)s))", {"key": key})


def check_database_connection(config: DatabaseConfig | None = None) -> bool:
    """Attempt a real ``SELECT 1`` against PostgreSQL.

    Returns True on success. Raises :class:`DatabaseNotConfiguredError`
    when settings are incomplete and :class:`DatabaseUnavailableError`
    for any connection/query failure.
    """
    cfg = config if config is not None else get_database_config()
    if not cfg.is_configured:
        raise DatabaseNotConfiguredError("database_not_configured")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency present via requirements
        raise DatabaseUnavailableError("driver_unavailable") from exc
    try:
        with psycopg.connect(**cfg.connection_kwargs()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
    except Exception as exc:
        raise DatabaseUnavailableError("connection_failed") from exc
    return bool(row is not None and row[0] == 1)
