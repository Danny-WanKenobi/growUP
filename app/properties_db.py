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

import json
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


def _property_insert_params(data: dict[str, Any]) -> dict[str, Any]:
    """Validate payload shape and build INSERT params (no I/O)."""
    declared = data.get("data_status")
    if declared is not None:
        marker = str(getattr(declared, "value", declared)).strip().lower()
        if marker == "hypothetical":
            raise ValueError("hypothetical values are not permitted in operational records")
    params = {c: data.get(c) for c in _INSERT_COLUMNS}
    params["id"] = str(uuid4())
    params["data_status"] = derive_data_status(params).value  # server-derived, never trusted
    # Store enum values as plain text.
    category = params.get("category")
    if category is not None and not isinstance(category, str):
        params["category"] = str(getattr(category, "value", category))
    # JSONB columns take serialized text (established codebase convention);
    # None stays None so unknown evidence remains SQL NULL.
    if params.get("field_evidence") is not None:
        params["field_evidence"] = json.dumps(params["field_evidence"])
    return params


def _insert_property_cursor(cur: Any, params: dict[str, Any]) -> dict[str, Any] | None:
    """Execute the property INSERT on a caller-managed cursor (no commit)."""
    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _INSERT_COLUMNS)
    sql = (
        f"INSERT INTO properties ({columns}) VALUES ({placeholders}) "
        f"RETURNING {', '.join(_PROPERTY_COLUMNS)}"
    )
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row is not None else None


def create_property(data: dict[str, Any], config: DatabaseConfig | None = None) -> dict[str, Any]:
    """Insert one operational property row and return it.

    Server assigns id/timestamps and derives ``data_status`` from the
    populated estimated/verified fields; a caller-provided label is
    never trusted. Raises ValueError when the payload declares a
    hypothetical status -- hypothetical values are rejected, never
    stored in the authoritative store.
    """
    params = _property_insert_params(data)
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                row = _insert_property_cursor(cur, params)
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    if row is None:  # pragma: no cover - defensive; INSERT..RETURNING always returns a row
        raise DatabaseUnavailableError("database_error")
    return row


def create_property_in_transaction(conn: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Insert one property on the caller's connection (no commit).

    Same validation and row shape as :func:`create_property`; the
    caller owns commit/rollback so several writes can share one
    transaction.
    """
    params = _property_insert_params(data)
    with conn.cursor() as cur:
        row = _insert_property_cursor(cur, params)
    if row is None:  # pragma: no cover - defensive; INSERT..RETURNING always returns a row
        raise DatabaseUnavailableError("database_error")
    return row


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


def _normalized_component_sql(column: str) -> str:
    """SQL mirror of gabriel ``normalize_text`` for one address component.

    Lowercases, maps ``[.,#/-]+`` to a space, collapses whitespace, and
    trims -- the same contract callers apply to incoming values, so
    equivalent spellings match without changing stored values. NULL
    stays NULL (never matches); abbreviations, ZIP text, suite numbers,
    and directionals are preserved exactly (never expanded or dropped).
    """
    return (
        "btrim(regexp_replace(regexp_replace(lower(btrim(" + column + "))"
        ", '[.,#/-]+', ' ', 'g'), '\\s+', ' ', 'g'))"
    )


def find_by_address(
    street: str,
    city: str = "",
    state: str = "",
    zip_code: str = "",
    config: DatabaseConfig | None = None,
) -> list[dict[str, Any]]:
    """Return the ids matching a normalized address (at most two).

    Stored components are normalized in SQL exactly as callers
    normalize inputs first (see gabriel dedup). Returns every match
    up to two rows: zero (no match), one (unique match), or two (more
    than one match exists -- ambiguous, never treated as unique).
    Callers must not select among multiple matches.
    """
    street_expr = _normalized_component_sql("street_address")
    city_expr = _normalized_component_sql("COALESCE(city, '')")
    state_expr = _normalized_component_sql("COALESCE(state, '')")
    zip_expr = _normalized_component_sql("COALESCE(zip_code, '')")
    sql = (
        "SELECT id FROM properties "
        f"WHERE {street_expr} = %(street)s "
        f"AND {city_expr} = %(city)s "
        f"AND {state_expr} = %(state)s "
        f"AND {zip_expr} = %(zip)s "
        "LIMIT 2"
    )
    params = {"street": street, "city": city, "state": state, "zip": zip_code}
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return [dict(row) for row in rows]


ENRICHABLE_ESTIMATED_FIELDS = (
    "building_area_sqft_estimated",
    "vacant_area_sqft_estimated",
    "gross_monthly_rent_income_estimated",
    "owner_monthly_costs_estimated",
    "property_value_estimated",
    "liens_total_estimated",
    "months_on_market_estimated",
    "months_vacant_estimated",
)


def enrich_property_estimates(
    property_id: UUID | str,
    fills: dict[str, Any],
    field_evidence: dict[str, str] | None,
    data_status: str,
    config: DatabaseConfig | None = None,
) -> list[str]:
    """Fill NULL estimated columns on an existing record (all-or-nothing).

    Only allowlisted estimated fields are accepted, and each is guarded
    by ``IS NULL`` so existing values -- verified or estimated -- are
    never overwritten. ``field_evidence`` replaces the stored evidence
    map (callers merge it first); ``data_status`` is the caller-derived
    status for the merged record. Returns the filled field names, or an
    empty list when the row changed concurrently (guards failed).
    """
    unknown = [key for key in fills if key not in ENRICHABLE_ESTIMATED_FIELDS]
    if unknown:
        raise ValueError(f"enrichment supports estimated fields only: {unknown}")
    if not fills:
        return []
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                row = _update_estimates_cursor(cur, property_id, fills, field_evidence, data_status)
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return list(fills) if row is not None else []


def _update_estimates_cursor(
    cur: Any,
    property_id: UUID | str,
    fills: dict[str, Any],
    field_evidence: dict[str, str] | None,
    data_status: str,
) -> dict[str, Any] | None:
    """Execute the guarded estimates UPDATE on a caller-managed cursor (no commit)."""
    assignments = ", ".join(f"{col} = %({col})s" for col in fills)
    guards = " AND ".join(f"{col} IS NULL" for col in fills)
    sql = (
        f"UPDATE properties SET {assignments}, "
        "field_evidence = %(field_evidence)s, data_status = %(data_status)s, "
        "updated_at = now() "
        f"WHERE id = %(id)s AND {guards} "
        "RETURNING id"
    )
    params = {"id": str(property_id),
              "field_evidence": json.dumps(field_evidence) if field_evidence is not None else None,
              "data_status": data_status, **fills}
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row is not None else None


def enrich_property_estimates_in_transaction(
    conn: Any,
    property_id: UUID | str,
    fills: dict[str, Any],
    field_evidence: dict[str, str] | None,
    data_status: str,
) -> list[str]:
    """Fill NULL estimated columns on the caller's connection (no commit).

    Same guards and row shape as :func:`enrich_property_estimates`;
    the caller owns commit/rollback so several writes can share one
    transaction.
    """
    unknown = [key for key in fills if key not in ENRICHABLE_ESTIMATED_FIELDS]
    if unknown:
        raise ValueError(f"enrichment supports estimated fields only: {unknown}")
    if not fills:
        return []
    with conn.cursor() as cur:
        row = _update_estimates_cursor(cur, property_id, fills, field_evidence, data_status)
    return list(fills) if row is not None else []


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
