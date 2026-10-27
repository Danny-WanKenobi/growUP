"""Append-only commercial listing evidence storage (MVP-004A).

Holds listing details that cannot safely map to estimated/verified
property columns (asking rent, advertised owner, listing type/date,
price history) plus per-observation distress attribution. Rows are
inserts only -- intake never updates or deletes evidence -- and are
linked to the property they describe. Error handling follows the
``app.properties_db`` conventions (generic categories, no leaks).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection


def create_listing_evidence(
    property_id: str,
    source_name: str,
    source_url: str,
    observed_at: Any,
    listing_type: str,
    payload: dict[str, Any],
    config: Any = None,
) -> dict[str, Any]:
    """Insert one listing-evidence row and return its id."""
    sql = (
        "INSERT INTO listing_evidence "
        "(id, property_id, source_name, source_url, observed_at, listing_type, payload) "
        "VALUES (%(id)s, %(property_id)s, %(source_name)s, %(source_url)s, "
        "%(observed_at)s, %(listing_type)s, %(payload)s) "
        "RETURNING id"
    )
    params = {
        "id": str(uuid4()),
        "property_id": str(property_id),
        "source_name": source_name,
        "source_url": source_url,
        "observed_at": observed_at,
        "listing_type": listing_type,
        "payload": payload,
    }
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
    if row is None:  # pragma: no cover - defensive
        raise DatabaseUnavailableError("database_error")
    return dict(row)


def get_listing_evidence(
    property_id: str, source_url: str, config: Any = None
) -> dict[str, Any] | None:
    """Return the evidence row for a property+URL pair, or None.

    Intake uses this to avoid recording the same listing twice.
    """
    sql = (
        "SELECT id FROM listing_evidence "
        "WHERE property_id = %(property_id)s AND source_url = %(source_url)s "
        "LIMIT 1"
    )
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"property_id": str(property_id), "source_url": source_url})
                row = cur.fetchone()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return dict(row) if row is not None else None


def create_distress_observations(
    property_id: str, observations: list[dict[str, Any]], config: Any = None
) -> list[str]:
    """Insert distress-observation rows; returns their ids in order."""
    if not observations:
        return []
    sql = (
        "INSERT INTO distress_observations "
        "(id, property_id, indicator_type, source_name, source_url, "
        "observed_at, description, status) "
        "VALUES (%(id)s, %(property_id)s, %(indicator_type)s, %(source_name)s, "
        "%(source_url)s, %(observed_at)s, %(description)s, %(status)s) "
        "RETURNING id"
    )
    ids: list[str] = []
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                for obs in observations:
                    cur.execute(sql, {"id": str(uuid4()), "property_id": str(property_id), **obs})
                    row = cur.fetchone()
                    if row is None:  # pragma: no cover - defensive
                        raise DatabaseUnavailableError("database_error")
                    ids.append(str(row["id"] if isinstance(row, dict) else row[0]))
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return ids
