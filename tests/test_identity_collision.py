"""Same-address distinct-building identity collision repro (synthetic only).

Expected behavior (correct): two distinct physical buildings that share a
street address must NOT be silently merged, cross-enriched, or share one
property record's listing evidence.

These tests drive the REAL listing-intake workflow
(`app.agents.gabriel.intake.process_intake` with the real dedup matcher)
against a stateful in-memory stand-in for the PostgreSQL driver only. No
production code is modified, no external service is contacted, and all
data is synthetic.

Conservative guard behavior: a same-address listing with a materially
different building name is treated as ambiguous -- the existing record
is preserved unchanged, no evidence is attached, no second record is
created, and the result reports `needs_identity_review`. An exact
same-name, same-address listing from a second source still enriches.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.agents.gabriel.intake import ListingIntakeRequest, process_intake

_STORE: dict[str, Any] | None = None


class _CollisionStore:
    """Minimal in-memory stand-in for the properties/evidence tables."""

    def __init__(self) -> None:
        self.properties: dict[str, dict[str, Any]] = {}
        self.evidence: list[dict[str, Any]] = []


class _FakeCursor:
    def __init__(self, store: _CollisionStore) -> None:
        self._store = store
        self._query = ""
        self._params: dict[str, Any] = {}

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        self._query = query
        self._params = dict(params or {})

    def fetchone(self) -> dict[str, Any] | None:
        store = self._store
        query = self._query
        params = self._params
        if "FROM listing_evidence" in query and query.strip().upper().startswith("SELECT"):
            for row in store.evidence:
                if row["property_id"] == str(params["property_id"]) and row["source_url"] == params["source_url"]:
                    return {"id": row["id"]}
            return None
        if "INSERT INTO listing_evidence" in query:
            store.evidence.append(
                {
                    "id": params["id"],
                    "property_id": str(params["property_id"]),
                    "source_name": params["source_name"],
                    "source_url": params["source_url"],
                }
            )
            return {"id": params["id"]}
        if "INSERT INTO distress_observations" in query:
            return {"id": params["id"]}
        if "lower(btrim(street_address))" in query:
            # Faithful to app.properties_db.find_by_address: exact match on
            # lowercased/trimmed parts (no depunctuation, no unit handling).
            def _norm(value: object) -> str:
                return str(value or "").strip().lower()

            for pid, row in store.properties.items():
                if (
                    _norm(row.get("street_address")) == params["street"]
                    and _norm(row.get("city")) == params["city"]
                    and _norm(row.get("state")) == params["state"]
                    and _norm(row.get("zip_code")) == params["zip"]
                ):
                    return {"id": pid}
            return None
        if query.strip().upper().startswith("INSERT INTO PROPERTIES"):
            row = dict(params)
            row.setdefault("created_at", "2026-10-20T00:00:00+00:00")
            row.setdefault("updated_at", "2026-10-20T00:00:00+00:00")
            store.properties[str(params["id"])] = row
            return dict(row)
        if "FROM properties WHERE id" in query:
            row = store.properties.get(str(params["id"]))
            return dict(row) if row is not None else None
        if query.strip().upper().startswith("UPDATE PROPERTIES SET"):
            row = store.properties.get(str(params["id"]))
            if row is None:
                return None
            fills = {
                key: value
                for key, value in params.items()
                if key not in ("id", "field_evidence", "data_status")
            }
            # Faithful to enrich_property_estimates: all-or-nothing IS NULL guards.
            if any(row.get(col) is not None for col in fills):
                return None
            row.update(fills)
            row["field_evidence"] = params["field_evidence"]
            row["data_status"] = params["data_status"]
            return {"id": str(params["id"])}
        raise AssertionError(f"unexpected query in collision repro: {query[:100]!r}")

    def fetchall(self) -> list:
        return []


class _FakeConnection:
    def __init__(self, store: _CollisionStore) -> None:
        self._store = store

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._store)

    def commit(self) -> None:
        pass


@pytest.fixture
def collision_store(monkeypatch: pytest.MonkeyPatch) -> _CollisionStore:
    """Route the real DB-access functions at an in-memory store.

    Only the driver-level `get_connection` is stubbed (both import sites);
    `find_existing_id`, `create_property`, `get_property`,
    `enrich_property_estimates`, and the listing-evidence helpers all run
    their real code, including the real SQL text and real dedup logic.
    """
    store = _CollisionStore()
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "pw")
    monkeypatch.setattr(
        "app.properties_db.get_connection", lambda config=None: _FakeConnection(store)
    )
    monkeypatch.setattr(
        "app.listings_db.get_connection", lambda config=None: _FakeConnection(store)
    )
    return store


def _listing(
    name: str | None,
    url: str,
    area: float | None,
    months_listed: int | None = None,
    street: str = "500 Commerce Street",
) -> ListingIntakeRequest:
    body: dict[str, Any] = {
        "name": name,
        "street_address": street,
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
        "category": "downtown_commercial",
        "source_name": "Harborline Brokerage Records",
        "listing_url": url,
        "observed_at": "2026-10-20T12:00:00+00:00",
    }
    if area is not None:
        body["advertised_building_area_sqft"] = area
    if months_listed is not None:
        body["months_listed_for_sale"] = months_listed
    return ListingIntakeRequest.model_validate(body)


def _diagnostics(store: _CollisionStore) -> str:
    links = [(row["source_url"], row["property_id"]) for row in store.evidence]
    areas = {
        pid: row.get("building_area_sqft_estimated") for pid, row in store.properties.items()
    }
    names = {pid: row.get("name") for pid, row in store.properties.items()}
    return f"property_count={len(store.properties)} names={names} areas={areas} evidence_links={links}"


def test_same_address_distinct_buildings_are_not_merged(
    collision_store: _CollisionStore,
) -> None:
    """Building A and B share an address but are distinct buildings.

    Correct behavior: two property records, each holding only its own
    listing evidence and its own advertised building area.
    """
    result_a = process_intake(
        _listing("Commerce Tower A", "https://example.com/listings/commerce-tower-a", 120000)
    )
    result_b = process_intake(
        _listing("Commerce Tower B", "https://example.com/listings/commerce-tower-b", 240000)
    )
    assert result_a.outcome.value == "created"
    assert result_b.outcome.value == "needs_identity_review", _diagnostics(collision_store)
    assert result_b.property_id == result_a.property_id
    assert "human identity review required" in result_b.message
    assert result_b.filled_fields == []
    assert result_b.listing_evidence_id is None
    # Building A remains the only persisted property, untouched.
    assert len(collision_store.properties) == 1, _diagnostics(collision_store)
    assert (
        collision_store.properties[result_a.property_id]["building_area_sqft_estimated"] == 120000
    )
    # Building B's evidence is not attached to Building A.
    assert [row["source_url"] for row in collision_store.evidence] == [
        "https://example.com/listings/commerce-tower-a"
    ]


def test_same_address_distinct_building_area_does_not_cross_enrich_when_null(
    collision_store: _CollisionStore,
) -> None:
    """Building B's area must not fill Building A's NULL field.

    Correct behavior: when the stored record's estimated field is NULL,
    a same-address listing for a DIFFERENT building must not enrich it.
    """
    result_a = process_intake(
        _listing("Commerce Tower A", "https://example.com/listings/commerce-tower-a", None)
    )
    assert result_a.outcome.value == "created"
    assert (
        collision_store.properties[result_a.property_id]["building_area_sqft_estimated"] is None
    )
    result_b = process_intake(
        _listing("Commerce Tower B", "https://example.com/listings/commerce-tower-b", 240000)
    )
    assert result_b.outcome.value == "needs_identity_review", _diagnostics(collision_store)
    assert result_b.property_id == result_a.property_id
    assert "human identity review required" in result_b.message
    assert result_b.filled_fields == []
    assert result_b.listing_evidence_id is None
    assert len(collision_store.properties) == 1, _diagnostics(collision_store)
    assert (
        collision_store.properties[result_a.property_id]["building_area_sqft_estimated"] is None
    ), "CROSS-ENRICHMENT: Building B's area landed on Building A's record. " + _diagnostics(
        collision_store
    )
    assert [row["source_url"] for row in collision_store.evidence] == [
        "https://example.com/listings/commerce-tower-a"
    ]


def test_exact_same_name_same_address_second_source_enriches(
    collision_store: _CollisionStore,
) -> None:
    """A confident same-name, same-address match still enriches normally."""
    result_a = process_intake(
        _listing("Commerce Tower A", "https://example.com/listings/commerce-tower-a", None)
    )
    assert result_a.outcome.value == "created"
    result_b = process_intake(
        _listing("Commerce Tower A", "https://example.com/listings/commerce-tower-a-broker-b", 240000)
    )
    assert result_b.outcome.value == "enriched", _diagnostics(collision_store)
    assert result_b.property_id == result_a.property_id
    assert result_b.filled_fields == ["building_area_sqft_estimated"]
    assert len(collision_store.properties) == 1
    assert (
        collision_store.properties[result_a.property_id]["building_area_sqft_estimated"] == 240000
    )
    assert sorted(row["source_url"] for row in collision_store.evidence) == [
        "https://example.com/listings/commerce-tower-a",
        "https://example.com/listings/commerce-tower-a-broker-b",
    ]
    # Case/punctuation/whitespace variants are not material differences.
    result_c = process_intake(
        _listing(
            "  COMMERCE tower A. ",
            "https://example.com/listings/commerce-tower-a-broker-c",
            None,
            months_listed=6,
        )
    )
    assert result_c.outcome.value == "enriched", _diagnostics(collision_store)
    assert (
        collision_store.properties[result_a.property_id]["months_on_market_estimated"] == 6
    )


# --- Live PostgreSQL integration (GROWUP_PG_INTEGRATION=1) ---
#
# Same gate and conventions as the existing live-DB tests (e.g.
# tests/test_observability_transactions.py): ambient DATABASE_* config,
# synthetic rows only, persisted-row verification (not just API objects),
# and try/finally cleanup by property id (listing evidence cascades via
# ON DELETE CASCADE). The default suite skips these without a server.


_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _live_config():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _require_live_tables() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.properties') AS props, "
                "to_regclass('public.listing_evidence') AS ev"
            )
            row = cur.fetchone()
    if row is None or row["props"] is None or row["ev"] is None:
        pytest.skip("properties/listing_evidence tables not present (migrations not applied)")


def _live_cleanup(property_ids: list[str]) -> None:
    from app.properties_db import get_connection

    ids = [str(item) for item in property_ids if item]
    if not ids:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM properties WHERE id = ANY(%(ids)s)", {"ids": ids})
        conn.commit()


def _live_property(property_id: str) -> dict[str, Any] | None:
    from app.properties_db import get_property

    return get_property(UUID(str(property_id)))


def _live_property_count(property_ids: list[str]) -> int:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM properties WHERE id = ANY(%(ids)s)",
                {"ids": [str(item) for item in property_ids]},
            )
            row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _live_evidence_for(property_id: str) -> list[dict[str, Any]]:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, property_id, source_url FROM listing_evidence "
                "WHERE property_id = %(id)s",
                {"id": str(property_id)},
            )
            return [dict(row) for row in cur.fetchall()]


def _live_evidence_by_url(url: str) -> list[dict[str, Any]]:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, property_id, source_url FROM listing_evidence "
                "WHERE source_url = %(url)s",
                {"url": url},
            )
            return [dict(row) for row in cur.fetchall()]


@needs_live_pg
def test_live_same_address_name_conflict_needs_review() -> None:
    """Live PG: conflicting names at one address are not merged or enriched."""
    _live_config()
    _require_live_tables()
    street = f"500 Identity Collision Way {uuid4().hex}"
    url_a = "https://example.com/listings/live-collision-a"
    url_b = "https://example.com/listings/live-collision-b"
    created: list[str] = []
    try:
        result_a = process_intake(_listing("Live Collision Tower A", url_a, 120000, street=street))
        assert result_a.outcome.value == "created"
        created.append(result_a.property_id)
        before = _live_property(result_a.property_id)
        assert before is not None
        assert float(before["building_area_sqft_estimated"] or 0) == 120000
        result_b = process_intake(_listing("Live Collision Tower B", url_b, 240000, street=street))
        assert result_b.outcome.value == "needs_identity_review"
        assert result_b.property_id == result_a.property_id
        assert "human identity review required" in result_b.message
        # Persisted rows: one property, unchanged; B's evidence nowhere stored.
        assert _live_property_count(created) == 1
        assert _live_property(result_a.property_id) == before
        assert sorted(row["source_url"] for row in _live_evidence_for(result_a.property_id)) == [url_a]
        assert _live_evidence_by_url(url_b) == []
    finally:
        _live_cleanup(created)


@needs_live_pg
def test_live_name_conflict_does_not_fill_null_area() -> None:
    """Live PG: a conflicting-name listing never fills the stored NULL area."""
    _live_config()
    _require_live_tables()
    street = f"700 Identity Collision Way {uuid4().hex}"
    url_a = "https://example.com/listings/live-collision-null-a"
    url_b = "https://example.com/listings/live-collision-null-b"
    created: list[str] = []
    try:
        result_a = process_intake(_listing("Live Collision Tower A", url_a, None, street=street))
        assert result_a.outcome.value == "created"
        created.append(result_a.property_id)
        stored = _live_property(result_a.property_id)
        assert stored is not None and stored["building_area_sqft_estimated"] is None
        result_b = process_intake(_listing("Live Collision Tower B", url_b, 240000, street=street))
        assert result_b.outcome.value == "needs_identity_review"
        reread = _live_property(result_a.property_id)
        assert reread is not None and reread["building_area_sqft_estimated"] is None
        assert _live_property_count(created) == 1
        assert sorted(row["source_url"] for row in _live_evidence_for(result_a.property_id)) == [url_a]
        assert _live_evidence_by_url(url_b) == []
    finally:
        _live_cleanup(created)


@needs_live_pg
def test_live_same_name_second_source_enriches() -> None:
    """Live PG: a confident same-name match still enriches persisted rows."""
    _live_config()
    _require_live_tables()
    street = f"900 Identity Accord Street {uuid4().hex}"
    url_a = "https://example.com/listings/live-accord-a"
    url_b = "https://example.com/listings/live-accord-b"
    created: list[str] = []
    try:
        result_a = process_intake(_listing("Live Accord Tower", url_a, None, street=street))
        assert result_a.outcome.value == "created"
        created.append(result_a.property_id)
        result_b = process_intake(_listing("Live Accord Tower", url_b, 240000, street=street))
        assert result_b.outcome.value == "enriched"
        assert result_b.property_id == result_a.property_id
        assert result_b.filled_fields == ["building_area_sqft_estimated"]
        stored = _live_property(result_a.property_id)
        assert stored is not None
        assert float(stored["building_area_sqft_estimated"] or 0) == 240000
        assert _live_property_count(created) == 1
        assert sorted(row["source_url"] for row in _live_evidence_for(result_a.property_id)) == [
            url_a,
            url_b,
        ]
    finally:
        _live_cleanup(created)
