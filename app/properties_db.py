"""PostgreSQL access for operational property records (MVP-002).

All property SQL lives here so route handlers never duplicate queries.
Errors are mapped to the shared ``app.db`` exception types with generic
messages so responses and logs never leak credentials or driver details.

EVIDENCE POLICY: this module writes to the authoritative store, where
numeric facts are explicit ESTIMATED / VERIFIED pairs and hypothetical
values are rejected. ``create_property`` derives ``data_status``
server-side (verified / estimated / mixed / unknown) and never trusts
a caller-provided label. Provenance/evidence sourcing rules are
enforced by the Pydantic gate; the application is the sole writer.
Monetary values travel as Decimal end to end (NUMERIC columns); they
are never converted through float.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from app.config import DatabaseConfig, get_database_config
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties import derive_data_status

_PROPERTY_COLUMNS = (
    "id",
    "name",
    "street_address",
    "city",
    "state",
    "zip_code",
    "category",
    "building_area_sqft_estimated",
    "building_area_sqft_verified",
    "vacant_area_sqft_estimated",
    "vacant_area_sqft_verified",
    "occupancy",
    "gross_monthly_rent_income_estimated",
    "gross_monthly_rent_income_verified",
    "owner",
    "owner_monthly_costs_estimated",
    "owner_monthly_costs_verified",
    "property_value_estimated",
    "property_value_verified",
    "liens_total_estimated",
    "liens_total_verified",
    "months_on_market_estimated",
    "months_on_market_verified",
    "months_vacant_estimated",
    "months_vacant_verified",
    "provenance",
    "field_evidence",
    "data_status",
    "notes",
    "created_at",
    "updated_at",
)

_INSERT_COLUMNS = ["id", *[c for c in _PROPERTY_COLUMNS if c not in ("id", "created_at", "updated_at")]]

MAX_LIST_LIMIT = 500


def get_connection(config: DatabaseConfig | None = None):  # type: ignore[no-untyped-def]
    """Open a PostgreSQL connection using the MVP-001 configuration."""
    cfg = config if config is not None else get_database_config()
    if not cfg.is_configured:
        raise DatabaseNotConfiguredError("database_not_configured")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency present via requirements
        raise DatabaseUnavailableError("driver_unavailable") from exc
    try:
        return psycopg.connect(**cfg.connection_kwargs(), row_factory=dict_row)
    except Exception as exc:
        raise DatabaseUnavailableError("connection_failed") from exc


def create_property(data: dict[str, Any], config: DatabaseConfig | None = None) -> dict[str, Any]:
    """Insert one operational property row and return it.

    Server assigns id/timestamps and derives ``data_status`` from the
    populated estimated/verified fields; a caller-provided label is
    never trusted. Raises ValueError when the payload declares a
    hypothetical status -- hypothetical values are rejected, never
    stored in the authoritative store.
    """
    declared = data.get("data_status")
    if declared is not None:
        marker = str(getattr(declared, "value", declared)).strip().lower()
        if marker == "hypothetical":
            raise ValueError("hypothetical values are not permitted in operational records")
    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _INSERT_COLUMNS)
    params = {c: data.get(c) for c in _INSERT_COLUMNS}
    params["id"] = str(uuid4())
    params["data_status"] = derive_data_status(params).value  # server-derived, never trusted
    # Store enum values as plain text.
    category = params.get("category")
    if category is not None and not isinstance(category, str):
        params["category"] = str(getattr(category, "value", category))
    sql = (
        f"INSERT INTO properties ({columns}) VALUES ({placeholders}) "
        f"RETURNING {', '.join(_PROPERTY_COLUMNS)}"
    )
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    if row is None:  # pragma: no cover - defensive; INSERT..RETURNING always returns a row
        raise DatabaseUnavailableError("database_error")
    return dict(row)


def list_properties(
    limit: int = 100, offset: int = 0, config: DatabaseConfig | None = None
) -> list[dict[str, Any]]:
    """Return stored properties, newest first."""
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    offset = max(0, offset)
    sql = (
        f"SELECT {', '.join(_PROPERTY_COLUMNS)} FROM properties "
        "ORDER BY created_at DESC, id DESC LIMIT %(limit)s OFFSET %(offset)s"
    )
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"limit": limit, "offset": offset})
                rows = cur.fetchall()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return [dict(row) for row in rows]


def get_property(property_id: UUID, config: DatabaseConfig | None = None) -> dict[str, Any] | None:
    """Return one property row by id, or None when it does not exist."""
    sql = f"SELECT {', '.join(_PROPERTY_COLUMNS)} FROM properties WHERE id = %(id)s"
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"id": str(property_id)})
                row = cur.fetchone()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return dict(row) if row is not None else None
