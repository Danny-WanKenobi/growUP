"""Append-only commercial listing evidence storage (MVP-004A).

Holds listing details that cannot safely map to estimated/verified
property columns (asking rent, advertised owner, listing type/date,
price history) plus per-observation distress attribution. Rows are
inserts only -- intake never updates or deletes evidence -- and are
linked to the property they describe. Error handling follows the
``app.properties_db`` conventions (generic categories, no leaks).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection


def _insert_evidence_cursor(
    cur: Any,
    property_id: str,
    source_name: str,
    source_url: str,
    observed_at: Any,
    listing_type: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Execute the evidence INSERT on a caller-managed cursor (no commit)."""
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
        "payload": json.dumps(payload),  # JSONB takes serialized text (codebase convention)
    }
    cur.execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row is not None else None


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
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                row = _insert_evidence_cursor(
                    cur, property_id, source_name, source_url, observed_at, listing_type, payload
                )
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    if row is None:  # pragma: no cover - defensive
        raise DatabaseUnavailableError("database_error")
    return row


def create_listing_evidence_in_transaction(
    conn: Any,
    property_id: str,
    source_name: str,
    source_url: str,
    observed_at: Any,
    listing_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Insert one evidence row on the caller's connection (no commit).

    Same row shape as :func:`create_listing_evidence`; the caller owns
    commit/rollback so several writes can share one transaction.
    """
    with conn.cursor() as cur:
        row = _insert_evidence_cursor(
            cur, property_id, source_name, source_url, observed_at, listing_type, payload
        )
    if row is None:  # pragma: no cover - defensive
        raise DatabaseUnavailableError("database_error")
    return row


def find_evidence_by_url(source_url: str, config: Any = None) -> list[dict[str, Any]]:
    """Return the distinct properties linked to a source URL (at most two).

    Exact URL match only -- no canonicalization, no fuzzy matching.
    Zero rows (no association), one row (single association), or two
    rows (more than one association exists -- ambiguous, never treated
    as unique). Callers must not infer property identity from a URL.
    """
    sql = (
        "SELECT DISTINCT property_id FROM listing_evidence "
        "WHERE source_url = %(source_url)s "
        "LIMIT 2"
    )
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"source_url": source_url})
                rows = cur.fetchall()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return [dict(row) for row in rows]


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


def _insert_distress_cursor(
    cur: Any, property_id: str, observations: list[dict[str, Any]]
) -> list[str]:
    """Execute distress INSERTs on a caller-managed cursor (no commit)."""
    sql = (
        "INSERT INTO distress_observations "
        "(id, property_id, indicator_type, source_name, source_url, "
        "observed_at, description, status) "
        "VALUES (%(id)s, %(property_id)s, %(indicator_type)s, %(source_name)s, "
        "%(source_url)s, %(observed_at)s, %(description)s, %(status)s) "
        "RETURNING id"
    )
    ids: list[str] = []
    for obs in observations:
        cur.execute(sql, {"id": str(uuid4()), "property_id": str(property_id), **obs})
        row = cur.fetchone()
        if row is None:  # pragma: no cover - defensive
            raise DatabaseUnavailableError("database_error")
        ids.append(str(row["id"] if isinstance(row, dict) else row[0]))
    return ids


def create_distress_observations(
    property_id: str, observations: list[dict[str, Any]], config: Any = None
) -> list[str]:
    """Insert distress-observation rows; returns their ids in order."""
    if not observations:
        return []
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                ids = _insert_distress_cursor(cur, property_id, observations)
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return ids


def create_distress_observations_in_transaction(
    conn: Any, property_id: str, observations: list[dict[str, Any]]
) -> list[str]:
    """Insert distress rows on the caller's connection (no commit).

    Same row shape as :func:`create_distress_observations`; the caller
    owns commit/rollback so several writes can share one transaction.
    """
    if not observations:
        return []
    with conn.cursor() as cur:
        return _insert_distress_cursor(cur, property_id, observations)
