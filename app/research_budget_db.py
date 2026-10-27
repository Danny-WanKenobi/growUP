"""PostgreSQL-backed Brave research budget accounting (MVP-004B-2).

Tracks metered Brave Search API usage per UTC calendar month. Capacity
is reserved with a single conditional UPDATE (atomic under Postgres row
locking), so concurrent runs cannot overspend. Reservations happen
before external requests and are never refunded: timeouts and provider
errors still count, and HTTP errors are never assumed free. Budget
state lives in the database and survives service restarts.

Ceiling: ``BRAVE_MONTHLY_CEILING`` env var, default 100. The live
configured value is enforced; the row stores the ceiling in effect when
it was created, for audit.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection

BUDGET_ENV_VAR = "BRAVE_MONTHLY_CEILING"
DEFAULT_MONTHLY_CEILING = 100


class BudgetExhausted(Exception):
    """Insufficient Brave request capacity remains this month."""


class BudgetConfigError(ValueError):
    """The budget ceiling configuration is missing or invalid."""


def current_month_key() -> str:
    """UTC calendar-month key (``YYYY-MM``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_monthly_ceiling() -> int:
    """Read the configured ceiling; raises BudgetConfigError when invalid."""
    raw = os.getenv(BUDGET_ENV_VAR, str(DEFAULT_MONTHLY_CEILING)).strip()
    try:
        ceiling = int(raw)
    except ValueError as exc:
        raise BudgetConfigError("invalid Brave monthly ceiling") from exc
    if ceiling < 1:
        raise BudgetConfigError("invalid Brave monthly ceiling")
    return ceiling


def read_budget(config: Any = None) -> dict[str, Any]:
    """Read current-month usage without reserving capacity."""
    ceiling = get_monthly_ceiling()
    month = current_month_key()
    sql = "SELECT used FROM research_budget WHERE research_month = %(month)s"
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"month": month})
                row = cur.fetchone()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    used = 0
    if row is not None:
        used = row["used"] if isinstance(row, dict) else row[0]
    return {"used": used, "ceiling": ceiling, "remaining": ceiling - used, "month": month}


def reserve_requests(count: int, config: Any = None) -> dict[str, Any]:
    """Atomically reserve Brave request capacity for the current month.

    Returns ``{"month", "used", "ceiling", "remaining"}``. Raises
    BudgetExhausted when capacity is insufficient; the reservation is
    all-or-nothing and counts attempts, not successes.
    """
    if count < 1:
        raise ValueError("reservation count must be positive")
    ceiling = get_monthly_ceiling()
    month = current_month_key()
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO research_budget (research_month, used, ceiling) "
                    "VALUES (%(month)s, 0, %(ceiling)s) "
                    "ON CONFLICT (research_month) DO NOTHING",
                    {"month": month, "ceiling": ceiling},
                )
                cur.execute(
                    "UPDATE research_budget SET used = used + %(count)s, updated_at = now() "
                    "WHERE research_month = %(month)s AND used + %(count)s <= %(ceiling)s "
                    "RETURNING used",
                    {"month": month, "count": count, "ceiling": ceiling},
                )
                row = cur.fetchone()
            if row is None:
                conn.rollback()
            else:
                conn.commit()
    except BudgetExhausted:
        raise
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    if row is None:
        raise BudgetExhausted(f"insufficient Brave budget for {count} request(s)")
    used = row["used"] if isinstance(row, dict) else row[0]
    return {"used": used, "ceiling": ceiling, "remaining": ceiling - used, "month": month}
