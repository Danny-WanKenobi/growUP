"""Persistent source-observation tracking for Gabriel discovery (MVP-008).

A source observation records that one provider object (e.g. OSM element
"way/1001") was resolved to one operational property. Observations are
lookup aids, never identity proof: callers must still run the address
safeguards, and a recorded link never authorizes merging, reassigning,
or overwriting properties.

Canonical namespaces collapse provider aliases that describe the same
object domain: both Overpass and the OSM API observe OSM elements, so
both map to ``osm``. Unknown providers map to None and are never
tracked (existing behavior preserved).

Concurrency: ``record_observation_in_transaction`` uses INSERT ...
ON CONFLICT DO NOTHING RETURNING so simultaneous runs observing the
same new element serialize on the UNIQUE constraint; the loser sees
no RETURNING row and must roll back. Error handling follows the
``app.properties_db`` conventions (generic categories, no leaks).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection

# Provider name -> canonical observation namespace. Exact match only;
# anything unlisted is untracked (never silently treated as OSM).
_CANONICAL_SOURCES = {
    "overpass": "osm",
    "osm_api": "osm",
}


def canonical_source(provider: object) -> str | None:
    """Map a provider name to its canonical namespace, or None if unknown."""
    if not isinstance(provider, str):
        return None
    return _CANONICAL_SOURCES.get(provider)


def find_observation(
    source: str, external_id: str, config: Any = None
) -> dict[str, Any] | None:
    """Return the recorded link for a source object, or None."""
    sql = (
        "SELECT id, property_id FROM source_observations "
        "WHERE source = %(source)s AND external_id = %(external_id)s "
        "LIMIT 1"
    )
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"source": source, "external_id": external_id})
                row = cur.fetchone()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("database_error") from exc
    return dict(row) if row is not None else None


def record_observation_in_transaction(
    conn: Any, source: str, external_id: str, property_id: str
) -> dict[str, Any] | None:
    """Claim a source-object link inside the caller's transaction.

    Returns the inserted row, or None when another transaction already
    claimed this source object (caller must roll back). Never commits;
    the caller owns the transaction.
    """
    sql = (
        "INSERT INTO source_observations (id, source, external_id, property_id) "
        "VALUES (%(id)s, %(source)s, %(external_id)s, %(property_id)s) "
        "ON CONFLICT (source, external_id) DO NOTHING "
        "RETURNING id, property_id"
    )
    with conn.cursor() as cur:
        cur.execute(
            sql,
            {
                "id": str(uuid4()),
                "source": source,
                "external_id": external_id,
                "property_id": str(property_id),
            },
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None
