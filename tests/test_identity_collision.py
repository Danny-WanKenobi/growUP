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

import copy
import json
import os
from datetime import datetime, timezone
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
        self.distress: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.reviews: dict[str, dict[str, Any]] = {}
        self.review_occurrences: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.fail_on: str | None = None


def _pg_row(row: dict[str, Any]) -> dict[str, Any]:
    """Emulate PostgreSQL JSONB round-trip on reads.

    Real psycopg 3 parses JSONB columns back into Python objects on
    SELECT, so a serialized ``field_evidence`` parameter loads back as a
    dict. The in-memory store keeps params verbatim; this parses JSON
    text back on row reads to match server behavior.
    """
    out = dict(row)
    if isinstance(out.get("field_evidence"), str):
        out["field_evidence"] = json.loads(out["field_evidence"])
    return out


class _FakeCursor:
    def __init__(self, store: _CollisionStore) -> None:
        self._store = store
        self._query = ""
        self._params: dict[str, Any] = {}
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, query: str, params: dict[str, Any] | None = None) -> None:
        self._store.queries.append(query)
        self._store.params.append(dict(params or {}))
        if self._store.fail_on and self._store.fail_on in query:
            raise RuntimeError(f"injected failure for test: {self._store.fail_on}")
        self._query = query
        self._params = dict(params or {})
        self._rows = self._apply()

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def _apply(self) -> list[dict[str, Any]]:
        store = self._store
        query = self._query
        params = self._params
        if "pg_advisory_xact_lock" in query:
            return [{"locked": True}]
        if "FROM listing_evidence" in query and query.strip().upper().startswith("SELECT"):
            if "property_id" in params:
                for row in store.evidence:
                    if row["property_id"] == str(params["property_id"]) and row["source_url"] == params["source_url"]:
                        return [{"id": row["id"]}]
                return []
            # Global URL lookup: distinct property ids, at most two (LIMIT 2).
            seen: list[str] = []
            for row in store.evidence:
                if row["source_url"] == params["source_url"] and row["property_id"] not in seen:
                    seen.append(row["property_id"])
            return [{"property_id": pid} for pid in seen[:2]]
        if "INSERT INTO listing_evidence" in query:
            store.evidence.append(
                {
                    "id": params["id"],
                    "property_id": str(params["property_id"]),
                    "source_name": params["source_name"],
                    "source_url": params["source_url"],
                }
            )
            return [{"id": params["id"]}]
        if "INSERT INTO distress_observations" in query:
            store.distress.append(
                {"id": params["id"], "property_id": str(params["property_id"])}
            )
            return [{"id": params["id"]}]
        if (
            "FROM properties" in query
            and "WHERE id" not in query
            and query.strip().upper().startswith("SELECT")
        ):
            # Faithful to app.properties_db.find_by_address: stored parts
            # normalized exactly like incoming values (shared normalize_text
            # is idempotent, so re-normalizing the stored side mirrors SQL),
            # returning every match up to two rows (LIMIT 2).
            from app.agents.gabriel.dedup import normalize_text as _norm

            matches = []
            for pid, row in store.properties.items():
                if row.get("street_address") is None:
                    continue  # SQL NULL never matches
                if (
                    _norm(row.get("street_address")) == params["street"]
                    and _norm(row.get("city")) == params["city"]
                    and _norm(row.get("state")) == params["state"]
                    and _norm(row.get("zip_code")) == params["zip"]
                ):
                    matches.append({"id": pid})
            return matches[:2]
        if query.strip().upper().startswith("INSERT INTO PROPERTIES"):
            row = dict(params)
            row.setdefault("created_at", "2026-10-20T00:00:00+00:00")
            row.setdefault("updated_at", "2026-10-20T00:00:00+00:00")
            store.properties[str(params["id"])] = row
            return [_pg_row(row)]
        if "FROM properties WHERE id" in query:
            row = store.properties.get(str(params["id"]))
            return [_pg_row(row)] if row is not None else []
        if query.strip().upper().startswith("UPDATE PROPERTIES SET"):
            row = store.properties.get(str(params["id"]))
            if row is None:
                return []
            fills = {
                key: value
                for key, value in params.items()
                if key not in ("id", "field_evidence", "data_status")
            }
            # Faithful to enrich_property_estimates: all-or-nothing IS NULL guards.
            if any(row.get(col) is not None for col in fills):
                return []
            row.update(fills)
            row["field_evidence"] = params["field_evidence"]
            row["data_status"] = params["data_status"]
            return [{"id": str(params["id"])}]
        if "INSERT INTO agent_events" in query:
            store.events.append(dict(params))
            return []
        if "INSERT INTO identity_review_occurrences" in query:
            store.review_occurrences.append(
                {
                    "occurrence_id": params["occurrence_id"],
                    "review_id": str(params["review_id"]),
                    "event_id": params["event_id"],
                }
            )
            return [{"occurrence_id": params["occurrence_id"]}]
        if "INSERT INTO identity_reviews" in query:
            # Mirrors the partial-unique PENDING upsert in migration 009.
            key = params["dedup_key"]
            existing = store.reviews.get(key)
            if existing is not None and existing["status"] == "PENDING":
                existing["occurrence_count"] += 1
                existing["last_event_id"] = params["decision_event_id"]
                return [{"review_id": existing["review_id"]}]
            row = {
                "review_id": params["review_id"],
                "dedup_key": key,
                "source": params["source"],
                "reason_code": params["reason_code"],
                "status": "PENDING",
                "occurrence_count": 1,
                "decision_event_id": params["decision_event_id"],
                "last_event_id": params["decision_event_id"],
            }
            store.reviews[key] = row
            return [{"review_id": row["review_id"]}]
        raise AssertionError(f"unexpected query in collision repro: {query[:100]!r}")


class _FakeConnection:
    def __init__(self, store: _CollisionStore) -> None:
        self._store = store
        self._snapshot: tuple | None = None
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> _FakeConnection:
        self._snapshot = copy.deepcopy(
            (
                self._store.properties,
                self._store.evidence,
                self._store.distress,
                self._store.events,
                self._store.reviews,
                self._store.review_occurrences,
            )
        )
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._store)

    def commit(self) -> None:
        self.commits += 1
        self._snapshot = None

    def rollback(self) -> None:
        self.rollbacks += 1
        if self._snapshot is not None:
            properties, evidence, distress, events, reviews, occurrences = copy.deepcopy(
                self._snapshot
            )
            self._store.properties = properties
            self._store.evidence = evidence
            self._store.distress = distress
            self._store.events = events
            self._store.reviews = reviews
            self._store.review_occurrences = occurrences
            self._snapshot = None


@pytest.fixture
def collision_store(monkeypatch: pytest.MonkeyPatch) -> _CollisionStore:
    """Route the real DB-access functions at an in-memory store.

    Only the driver-level `get_connection` is stubbed (all import sites,
    including the intake coordinator's); `match_existing_address`, the
    transaction-capable writers, and the listing-evidence helpers all run
    their real code, including the real SQL text and real dedup logic.
    Lock acquisition also runs its real code against the fake cursor.
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
    monkeypatch.setattr(
        "app.agents.gabriel.intake.get_connection", lambda config=None: _FakeConnection(store)
    )
    return store


def _listing(
    name: str | None,
    url: str,
    area: float | None,
    months_listed: int | None = None,
    street: str | None = "500 Commerce Street",
    city: str = "Dallas",
    zip_code: str | None = "75202",
    distress: list[dict[str, Any]] | None = None,
) -> ListingIntakeRequest:
    body: dict[str, Any] = {
        "name": name,
        "street_address": street,
        "city": city,
        "state": "TX",
        "zip_code": zip_code,
        "category": "downtown_commercial",
        "source_name": "Harborline Brokerage Records",
        "listing_url": url,
        "observed_at": "2026-10-20T12:00:00+00:00",
    }
    if area is not None:
        body["advertised_building_area_sqft"] = area
    if months_listed is not None:
        body["months_listed_for_sale"] = months_listed
    if distress is not None:
        body["distress_observations"] = distress
    return ListingIntakeRequest.model_validate(body)


_DISTRESS_ONE = [{"indicator_type": "other", "description": "Long enough description here."}]


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


def _seed_legacy_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    """Seed two same-normalized-address rows, bypassing intake matching.

    Simulates legacy duplicates (e.g. created before symmetric
    normalization): different raw spellings, different building names.
    """
    from app.properties_db import create_property

    base = {
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
        "category": "downtown_commercial",
        "provenance": "synthetic legacy seed record for multi-match tests",
    }
    row_a = create_property(
        {**base, "name": "Legacy Tower A", "street_address": "500 Legacy Pair Way"}
    )
    row_b = create_property(
        {**base, "name": "Legacy Tower B", "street_address": "500 Legacy Pair Way."}
    )
    return row_a, row_b


def test_named_listing_matching_two_records_needs_review(
    collision_store: _CollisionStore,
) -> None:
    """Even a name-matching listing is not resolved among two records."""
    row_a, row_b = _seed_legacy_pair()
    before_a = dict(collision_store.properties[str(row_a["id"])])
    before_b = dict(collision_store.properties[str(row_b["id"])])
    result = process_intake(
        _listing(
            "Legacy Tower A",
            "https://example.com/listings/multi-named",
            240000,
            street="500 Legacy Pair Way",
        )
    )
    assert result.outcome.value == "needs_identity_review", _diagnostics(collision_store)
    assert result.property_id == ""
    assert "multiple stored properties" in result.message
    assert "human identity review required" in result.message
    assert result.filled_fields == []
    assert result.listing_evidence_id is None
    assert len(collision_store.properties) == 2, _diagnostics(collision_store)
    assert collision_store.properties[str(row_a["id"])] == before_a
    assert collision_store.properties[str(row_b["id"])] == before_b
    assert collision_store.evidence == []


def test_nameless_listing_matching_two_records_needs_review(
    collision_store: _CollisionStore,
) -> None:
    """A nameless listing must not enrich an arbitrary matching record."""
    row_a, row_b = _seed_legacy_pair()
    before_a = dict(collision_store.properties[str(row_a["id"])])
    before_b = dict(collision_store.properties[str(row_b["id"])])
    result = process_intake(
        _listing(
            None,
            "https://example.com/listings/multi-nameless",
            240000,
            street="500 Legacy Pair Way",
        )
    )
    assert result.outcome.value == "needs_identity_review", _diagnostics(collision_store)
    assert result.property_id == ""
    assert "multiple stored properties" in result.message
    assert result.filled_fields == []
    assert result.listing_evidence_id is None
    assert len(collision_store.properties) == 2, _diagnostics(collision_store)
    assert collision_store.properties[str(row_a["id"])] == before_a
    assert collision_store.properties[str(row_b["id"])] == before_b
    assert collision_store.evidence == []


def test_addressless_url_resubmission_is_duplicate(
    collision_store: _CollisionStore,
) -> None:
    """Resubmitting an addressless URL creates nothing further."""
    url = "https://example.com/listings/url-resubmit"
    first = process_intake(_listing("URL Hall", url, None, street=None, distress=_DISTRESS_ONE))
    assert first.outcome.value == "created"
    assert len(collision_store.properties) == 1
    assert len(collision_store.evidence) == 1
    assert len(collision_store.distress) == 1
    second = process_intake(_listing("URL Hall", url, None, street=None, distress=_DISTRESS_ONE))
    assert second.outcome.value == "skipped_duplicate", _diagnostics(collision_store)
    assert second.property_id == first.property_id
    assert second.filled_fields == []
    assert second.listing_evidence_id is None
    assert second.distress_observation_ids == []
    assert len(collision_store.properties) == 1, _diagnostics(collision_store)
    assert len(collision_store.evidence) == 1
    assert len(collision_store.distress) == 1


def test_addressless_url_conflicting_identity_needs_review(
    collision_store: _CollisionStore,
) -> None:
    """A URL reused for a conflicting identity is reviewed, not merged."""
    url = "https://example.com/listings/url-conflict"
    first = process_intake(_listing("URL Hall A", url, None, street=None))
    assert first.outcome.value == "created"
    renamed = process_intake(_listing("URL Hall B", url, None, street=None))
    assert renamed.outcome.value == "needs_identity_review", _diagnostics(collision_store)
    assert renamed.property_id == ""
    assert "conflicting property identity" in renamed.message
    relocated = process_intake(_listing("URL Hall A", url, None, street=None, city="Irving"))
    assert relocated.outcome.value == "needs_identity_review", _diagnostics(collision_store)
    assert relocated.property_id == ""
    assert len(collision_store.properties) == 1, _diagnostics(collision_store)
    assert len(collision_store.evidence) == 1
    assert len(collision_store.distress) == 0


def test_addressless_url_multiple_properties_needs_review(
    collision_store: _CollisionStore,
) -> None:
    """A URL linked to two properties selects neither."""
    from app.listings_db import create_listing_evidence
    from app.properties_db import create_property

    url = "https://example.com/listings/url-multi"
    observed = datetime(2026, 10, 20, tzinfo=timezone.utc)
    base = {
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
        "category": "downtown_commercial",
        "provenance": "synthetic legacy seed record for URL multi-match tests",
    }
    row_a = create_property({**base, "name": "URL Hall A", "street_address": None})
    row_b = create_property({**base, "name": "URL Hall B", "street_address": None})
    for row in (row_a, row_b):
        create_listing_evidence(
            property_id=str(row["id"]),
            source_name="Legacy Seed Records",
            source_url=url,
            observed_at=observed,
            listing_type="unknown",
            payload={},
        )
    result = process_intake(_listing("URL Hall A", url, None, street=None))
    assert result.outcome.value == "needs_identity_review", _diagnostics(collision_store)
    assert result.property_id == ""
    assert "multiple properties" in result.message
    assert len(collision_store.properties) == 2, _diagnostics(collision_store)
    assert len(collision_store.evidence) == 2
    assert len(collision_store.distress) == 0


def test_addressless_unusable_url_rejected() -> None:
    """An addressless listing without a usable URL is rejected, not created."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _listing("URL Hall", "not-a-url", None, street=None)


def test_create_evidence_failure_rolls_back(collision_store: _CollisionStore) -> None:
    """Evidence failure leaves no property behind (real coordinator, fake driver)."""
    from app.db import DatabaseUnavailableError

    collision_store.fail_on = "INSERT INTO listing_evidence"
    with pytest.raises(DatabaseUnavailableError):
        process_intake(
            _listing("Atomic Hall", "https://example.com/listings/atomic-evidence-fail", 120000)
        )
    assert collision_store.properties == {}
    assert collision_store.evidence == []
    assert collision_store.distress == []


def test_create_distress_failure_rolls_back(collision_store: _CollisionStore) -> None:
    """Distress failure rolls back the property and evidence with it."""
    from app.db import DatabaseUnavailableError

    collision_store.fail_on = "INSERT INTO distress_observations"
    with pytest.raises(DatabaseUnavailableError):
        process_intake(
            _listing(
                "Atomic Hall",
                "https://example.com/listings/atomic-distress-fail",
                120000,
                distress=_DISTRESS_ONE,
            )
        )
    assert collision_store.properties == {}
    assert collision_store.evidence == []
    assert collision_store.distress == []


def test_enrich_evidence_failure_rolls_back(collision_store: _CollisionStore) -> None:
    """Enrichment failure leaves the stored property and evidence untouched."""
    from app.db import DatabaseUnavailableError

    first = process_intake(
        _listing("Atomic Hall", "https://example.com/listings/atomic-enrich-a", None)
    )
    assert first.outcome.value == "created"
    assert len(collision_store.evidence) == 1
    collision_store.fail_on = "INSERT INTO listing_evidence"
    with pytest.raises(DatabaseUnavailableError):
        process_intake(
            _listing("Atomic Hall", "https://example.com/listings/atomic-enrich-b", 240000)
        )
    assert collision_store.properties[first.property_id]["building_area_sqft_estimated"] is None
    assert len(collision_store.properties) == 1
    assert len(collision_store.evidence) == 1
    assert collision_store.distress == []


def test_addressed_url_on_different_property_needs_review(
    collision_store: _CollisionStore,
) -> None:
    """Address match and URL association disagreeing selects neither property."""
    first_a = process_intake(
        _listing("Hall Alpha", "https://example.com/listings/split-a", None)
    )
    assert first_a.outcome.value == "created"
    first_b = process_intake(
        _listing(
            "Hall Beta",
            "https://example.com/listings/split-b",
            None,
            street="700 Split Avenue",
        )
    )
    assert first_b.outcome.value == "created"
    result = process_intake(
        _listing(
            "Hall Beta",
            "https://example.com/listings/split-a",
            240000,
            street="700 Split Avenue",
        )
    )
    assert result.outcome.value == "needs_identity_review", _diagnostics(collision_store)
    assert result.property_id == ""
    assert "different property" in result.message
    assert len(collision_store.properties) == 2, _diagnostics(collision_store)
    assert len(collision_store.evidence) == 2


def test_addressed_new_street_known_url_duplicates_without_populating(
    collision_store: _CollisionStore,
) -> None:
    """An addressed listing of a known addressless URL duplicates; no backfill."""
    first = process_intake(
        _listing("Gain Hall", "https://example.com/listings/gain-url", None, street=None)
    )
    assert first.outcome.value == "created"
    assert collision_store.properties[first.property_id]["street_address"] is None
    result = process_intake(
        _listing(
            "Gain Hall",
            "https://example.com/listings/gain-url",
            240000,
            street="800 Gain Street",
        )
    )
    assert result.outcome.value == "skipped_duplicate", _diagnostics(collision_store)
    assert result.property_id == first.property_id
    assert len(collision_store.properties) == 1, _diagnostics(collision_store)
    assert len(collision_store.evidence) == 1
    assert collision_store.properties[first.property_id]["street_address"] is None


def test_intake_acquires_locks_before_writes(collision_store: _CollisionStore) -> None:
    """Both coordination keys lock (sorted) before decisions and writes."""
    url = "https://example.com/listings/atomic-lock-order"
    result = process_intake(_listing("Atomic Hall", url, 120000))
    assert result.outcome.value == "created"
    lock_params = [
        params["key"]
        for query, params in zip(collision_store.queries, collision_store.params)
        if "pg_advisory_xact_lock" in query
    ]
    assert lock_params == sorted(lock_params)
    assert len(lock_params) == 2
    assert lock_params[0].startswith("gabriel-intake-addr:")
    assert "500 commerce street" in lock_params[0]
    assert lock_params[1] == f"gabriel-intake-url:{url}"
    lock_positions = [
        i for i, query in enumerate(collision_store.queries) if "pg_advisory_xact_lock" in query
    ]
    decision_reads = [
        i
        for i, query in enumerate(collision_store.queries)
        if query.strip().upper().startswith("SELECT") and "pg_advisory_xact_lock" not in query
    ]
    assert decision_reads, "expected identity-decision SELECTs for an addressed intake"
    assert max(lock_positions) < min(decision_reads)
    first_write = next(
        i for i, query in enumerate(collision_store.queries) if query.startswith("INSERT INTO")
    )
    assert max(lock_positions) < first_write


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


def _live_cleanup_street(street: str) -> None:
    """Delete rows carrying this execution's unique synthetic street (exact match).

    Fallback for intake failures that commit a property row before its id
    is recorded for id-based cleanup. The street embeds a full per-run
    UUID and the match is exact -- no wildcards, no prefixes, no
    name-based deletion -- so only this execution's rows can match.
    Also removes this execution's intake decision events, matched by
    the same exact observed street.
    """
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM properties WHERE street_address = %(street)s",
                {"street": street},
            )
            cur.execute(
                "DELETE FROM agent_events WHERE event_type = 'gabriel.intake.decision' "
                "AND details->'observed'->>'street_address' = %(street)s",
                {"street": street},
            )
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


def _live_distress_count(property_id: str) -> int:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM distress_observations "
                "WHERE property_id = %(id)s",
                {"id": str(property_id)},
            )
            row = cur.fetchone()
    assert row is not None
    return int(row["n"])


def _live_cleanup_url(url: str) -> None:
    """Delete properties evidencing this execution's unique URL (exact match).

    Fallback for addressless tests, which have no street for
    street-scoped cleanup. The URL embeds a per-run UUID and the match
    is exact, so only this execution's rows can match. Evidence rows
    cascade from the property delete. Also removes this execution's
    intake decision events, matched by the same exact observed URL.
    """
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM properties WHERE id IN ("
                "SELECT property_id FROM listing_evidence WHERE source_url = %(url)s)",
                {"url": url},
            )
            cur.execute(
                "DELETE FROM agent_events WHERE event_type = 'gabriel.intake.decision' "
                "AND details->'observed'->>'listing_url' = %(url)s",
                {"url": url},
            )
        conn.commit()


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
        _live_cleanup_street(street)


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
        _live_cleanup_street(street)


@needs_live_pg
def test_live_equivalent_spellings_match_in_sql() -> None:
    """Live PG: punctuation, spacing, and case variants match one record.

    Same building name within each pair, so only address normalization
    decides: each B must enrich (not create, not review).
    """
    _live_config()
    _require_live_tables()
    punct_a = f"500 Norm Punct Way {uuid4().hex}."
    space_a = f"700  Norm   Space  Way {uuid4().hex}"
    case_a = f"900 norm case way {uuid4().hex}"
    # (label, street_a, street_b, city_a, city_b); B is the cleaned form of A.
    pairs = [
        ("punct", punct_a, " ".join(punct_a.rstrip(".").split()), "Dallas", "Dallas"),
        ("space", space_a, " ".join(space_a.split()), "Dallas", "Dallas"),
        ("case", case_a, " ".join(case_a.split()).upper(), "dallas", "DALLAS"),
    ]
    created: list[str] = []
    streets: list[str] = []
    try:
        for label, street_a, street_b, city_a, city_b in pairs:
            name = f"Live Norm {label.title()} Tower"
            result_a = process_intake(
                _listing(name, f"https://example.com/listings/live-norm-{label}-a", None,
                         street=street_a, city=city_a)
            )
            assert result_a.outcome.value == "created", label
            created.append(result_a.property_id)
            streets.append(street_a)
            result_b = process_intake(
                _listing(name, f"https://example.com/listings/live-norm-{label}-b", 240000,
                         street=street_b, city=city_b)
            )
            assert result_b.outcome.value == "enriched", (
                f"{label}: equivalent spellings must match "
                f"({street_a!r} vs {street_b!r})"
            )
            assert result_b.property_id == result_a.property_id, label
            stored = _live_property(result_a.property_id)
            assert stored is not None
            assert float(stored["building_area_sqft_estimated"] or 0) == 240000, label
        assert _live_property_count(created) == len(created)
    finally:
        _live_cleanup(created)
        for street in streets:
            _live_cleanup_street(street)


@needs_live_pg
def test_live_material_differences_do_not_match_in_sql() -> None:
    """Live PG: abbreviations, ZIP, suite, and directionals stay distinct.

    Same building name within each pair, so a match would enrich; each B
    must instead create its own record.
    """
    _live_config()
    _require_live_tables()
    base = uuid4().hex
    pairs = [
        ("abbrev", f"500 Norm {base} St", f"500 Norm {base} Street", "75202", "75202"),
        ("zip", f"600 Norm {base} Way", f"600 Norm {base} Way", "75202", None),
        ("suite", f"700 Norm {base} St Suite 100", f"700 Norm {base} St Suite 200",
         "75202", "75202"),
        ("directional", f"N Norm {base} St", f"Norm {base} St", "75202", "75202"),
    ]
    created: list[str] = []
    streets: list[str] = []
    try:
        for label, street_a, street_b, zip_a, zip_b in pairs:
            name = f"Live Distinct {label.title()} Tower"
            result_a = process_intake(
                _listing(name, f"https://example.com/listings/live-distinct-{label}-a", None,
                         street=street_a, zip_code=zip_a)
            )
            assert result_a.outcome.value == "created", label
            created.append(result_a.property_id)
            streets.append(street_a)
            result_b = process_intake(
                _listing(name, f"https://example.com/listings/live-distinct-{label}-b", 240000,
                         street=street_b, zip_code=zip_b)
            )
            assert result_b.outcome.value == "created", (
                f"{label}: material differences must not match "
                f"({street_a!r}/{zip_a} vs {street_b!r}/{zip_b})"
            )
            assert result_b.property_id != result_a.property_id, label
            created.append(result_b.property_id)
            streets.append(street_b)
        assert _live_property_count(created) == len(created) == 8
    finally:
        _live_cleanup(created)
        for street in streets:
            _live_cleanup_street(street)


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
        _live_cleanup_street(street)


@needs_live_pg
def test_live_multiple_matches_require_review() -> None:
    """Live PG: two same-normalized-address rows are never selected between.

    Seeds a legacy-style pair directly (different raw spellings, different
    names), then verifies named and nameless intakes both report ambiguity
    with zero persisted changes.
    """
    from app.properties_db import create_property

    _live_config()
    _require_live_tables()
    base = uuid4().hex
    street_a = f"500 Live Multi Way {base}."
    street_b = f"500 Live Multi Way {base}"
    name_a = "Live Multi Tower A"
    url_named = "https://example.com/listings/live-multi-named"
    url_nameless = "https://example.com/listings/live-multi-nameless"
    created: list[str] = []
    streets = [street_a, street_b]
    try:
        seed = {
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75202",
            "category": "downtown_commercial",
            "provenance": "synthetic live multi-match seed record QA",
        }
        row_a = create_property(
            {**seed, "name": name_a, "street_address": street_a,
             "building_area_sqft_estimated": 120000.0,
             "field_evidence": {"building_area_sqft_estimated": "live seed record"}}
        )
        row_b = create_property(
            {**seed, "name": "Live Multi Tower B", "street_address": street_b}
        )
        created.extend([str(row_a["id"]), str(row_b["id"])])
        before_a = _live_property(created[0])
        before_b = _live_property(created[1])
        assert before_a is not None and before_b is not None
        named = process_intake(_listing(name_a, url_named, 240000, street=street_b))
        assert named.outcome.value == "needs_identity_review"
        assert named.property_id == ""
        assert "multiple stored properties" in named.message
        nameless = process_intake(_listing(None, url_nameless, 99999, street=street_b))
        assert nameless.outcome.value == "needs_identity_review"
        assert nameless.property_id == ""
        assert "multiple stored properties" in nameless.message
        # Persisted rows: both unchanged, nothing created, nothing attached.
        assert _live_property_count(created) == 2
        assert _live_property(created[0]) == before_a
        assert _live_property(created[1]) == before_b
        assert _live_evidence_by_url(url_named) == []
        assert _live_evidence_by_url(url_nameless) == []
    finally:
        _live_cleanup(created)
        for street in streets:
            _live_cleanup_street(street)


@needs_live_pg
def test_live_addressless_url_resubmission_is_duplicate() -> None:
    """Live PG: resubmitting an addressless URL writes nothing further."""
    _live_config()
    _require_live_tables()
    url = f"https://example.com/listings/live-url-{uuid4().hex}"
    created: list[str] = []
    try:
        first = process_intake(
            _listing("Live URL Hall", url, None, street=None, distress=_DISTRESS_ONE)
        )
        assert first.outcome.value == "created"
        created.append(first.property_id)
        before = _live_property(first.property_id)
        assert before is not None
        second = process_intake(
            _listing("Live URL Hall", url, None, street=None, distress=_DISTRESS_ONE)
        )
        assert second.outcome.value == "skipped_duplicate"
        assert second.property_id == first.property_id
        assert _live_property_count(created) == 1
        assert _live_property(first.property_id) == before
        assert len(_live_evidence_by_url(url)) == 1
        assert _live_distress_count(first.property_id) == 1
    finally:
        _live_cleanup(created)
        _live_cleanup_url(url)
