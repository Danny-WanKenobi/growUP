"""Intake decision-provenance tests (G2 Phase A).

Every successfully completed intake request must produce exactly one
permanent ``gabriel.intake.decision`` event that records what was
observed, what was decided, why, and whether a property mutation
committed. Fake-driver tests prove branching and transaction shape;
live tests prove durable PostgreSQL persistence.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any
from uuid import uuid4

import pytest

from app.agents.gabriel import intake
from app.agents.gabriel.dedup import normalize_text as _norm
from app.agents.gabriel.intake import ListingIntakeRequest, process_intake
from app.db import DatabaseUnavailableError


class _Store:
    def __init__(self) -> None:
        self.properties: dict[str, dict[str, Any]] = {}
        self.evidence: list[dict[str, Any]] = []
        self.distress: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.reviews: dict[str, dict[str, Any]] = {}
        self.review_occurrences: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.conn_ids: list[int] = []
        self.fail_on: str | None = None
        self._next_conn = 0

    def events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("run_id") == run_id]


def _pg_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if isinstance(out.get("field_evidence"), str):
        out["field_evidence"] = json.loads(out["field_evidence"])
    return out


class _FakeCursor:
    def __init__(self, store: _Store, conn_id: int) -> None:
        self._store = store
        self._conn_id = conn_id
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, query: str, params: Any = None) -> None:
        store = self._store
        store.queries.append(query)
        store.params.append(dict(params or {}))
        store.conn_ids.append(self._conn_id)
        if store.fail_on and store.fail_on in query:
            raise Exception("injected failure")
        self._rows = self._apply(query, dict(params or {}))

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def _apply(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        store = self._store
        if "pg_advisory_xact_lock" in query:
            return [{"locked": True}]
        if "INSERT INTO agent_events" in query:
            store.events.append(dict(params))
            return []
        if "FROM listing_evidence" in query and query.strip().upper().startswith("SELECT"):
            if "property_id" in params:
                for row in store.evidence:
                    if row["property_id"] == str(params["property_id"]) and row["source_url"] == params["source_url"]:
                        return [{"id": row["id"]}]
                return []
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
            store.distress.append({"id": params["id"], "property_id": str(params["property_id"])})
            return [{"id": params["id"]}]
        if "FROM properties" in query and "WHERE id" not in query and query.strip().upper().startswith("SELECT"):
            matches = []
            for pid, row in store.properties.items():
                if row.get("street_address") is None:
                    continue
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
            fills = {k: v for k, v in params.items() if k not in ("id", "field_evidence", "data_status")}
            if any(row.get(col) is not None for col in fills):
                return []
            row.update(fills)
            row["field_evidence"] = params["field_evidence"]
            row["data_status"] = params["data_status"]
            return [{"id": str(params["id"])}]
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
        raise AssertionError(f"unexpected query in provenance repro: {query[:100]!r}")


class _FakeConnection:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self._snapshot: tuple | None = None
        store._next_conn += 1
        self._id = store._next_conn

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
        return _FakeCursor(self._store, self._id)

    def _mark(self, marker: str) -> None:
        self._store.queries.append(marker)
        self._store.params.append({})
        self._store.conn_ids.append(self._id)

    def commit(self) -> None:
        self._mark("--commit--")
        self._snapshot = None

    def rollback(self) -> None:
        self._mark("--rollback--")
        if self._snapshot is not None:
            props, ev, distress, events, reviews, occurrences = copy.deepcopy(self._snapshot)
            self._store.properties = props
            self._store.evidence = ev
            self._store.distress = distress
            self._store.events = events
            self._store.reviews = reviews
            self._store.review_occurrences = occurrences
            self._snapshot = None


@pytest.fixture
def prov_store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    """Route driver-level connections at an in-memory store with events."""
    store = _Store()
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "pw")
    monkeypatch.setattr("app.properties_db.get_connection", lambda config=None: _FakeConnection(store))
    monkeypatch.setattr("app.listings_db.get_connection", lambda config=None: _FakeConnection(store))
    monkeypatch.setattr("app.agents.gabriel.intake.get_connection", lambda config=None: _FakeConnection(store))
    return store


def _listing(
    name: str | None,
    url: str,
    area: float | None = None,
    street: str | None = "500 Commerce Street",
    city: str = "Dallas",
    **extra: Any,
) -> ListingIntakeRequest:
    body: dict[str, Any] = {
        "name": name,
        "street_address": street,
        "city": city,
        "state": "TX",
        "zip_code": "75202",
        "category": "commercial_property",
        "source_name": "Harborline Brokerage Records",
        "listing_url": url,
        "observed_at": "2026-10-20T12:00:00+00:00",
    }
    if area is not None:
        body["advertised_building_area_sqft"] = area
    body.update(extra)
    return ListingIntakeRequest.model_validate(body)


def _event_details(event: dict[str, Any]) -> dict[str, Any]:
    raw = event["details"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _positions(store: _Store, predicate) -> list[int]:
    return [i for i, query in enumerate(store.queries) if predicate(query)]


# 1. New property creation: same-transaction COMMITTED decision.


def test_create_records_committed_decision(prov_store: _Store) -> None:
    result = process_intake(_listing("Commerce Tower A", "https://example.com/listings/a", 120000))
    assert result.outcome.value == "created"
    assert result.reason_code == "NEW_IDENTITY"
    (event,) = prov_store.events_for_run(result.run_id)
    assert event["event_type"] == "gabriel.intake.decision"
    assert event["retention_class"] == "permanent"
    details = _event_details(event)
    assert details["outcome"] == "created"
    assert details["reason_code"] == "NEW_IDENTITY"
    assert details["mutation_status"] == "COMMITTED"
    assert details["result"]["property_id"] == result.property_id
    assert details["result"]["evidence_id"] == result.listing_evidence_id
    assert event["property_id"] == result.property_id
    prop_pos = _positions(prov_store, lambda q: q.startswith("INSERT INTO properties"))
    ev_pos = _positions(prov_store, lambda q: "INSERT INTO listing_evidence" in q)
    audit_pos = _positions(prov_store, lambda q: "INSERT INTO agent_events" in q)
    commit_pos = _positions(prov_store, lambda q: q == "--commit--")
    assert len(prop_pos) == len(ev_pos) == len(audit_pos) == 1
    assert prop_pos[0] < ev_pos[0] < audit_pos[0] < commit_pos[0]
    conn_ids = prov_store.conn_ids
    assert conn_ids[prop_pos[0]] == conn_ids[ev_pos[0]] == conn_ids[audit_pos[0]] == conn_ids[commit_pos[0]]


# 2. Enrichment with filled fields.


def test_enrich_records_commit_with_fills(prov_store: _Store) -> None:
    first = process_intake(_listing("Commerce Tower A", "https://example.com/listings/a"))
    second = process_intake(_listing("Commerce Tower A", "https://example.com/listings/b", 120000))
    assert second.outcome.value == "enriched"
    assert second.reason_code == "NULL_FILL"
    assert second.filled_fields == ["building_area_sqft_estimated"]
    (event,) = prov_store.events_for_run(second.run_id)
    details = _event_details(event)
    assert details["mutation_status"] == "COMMITTED"
    assert details["result"]["filled_fields"] == ["building_area_sqft_estimated"]
    assert details["considered_property_ids"] == [first.property_id]


# 3. Evidence-only enrichment.


def test_evidence_only_enrichment(prov_store: _Store) -> None:
    process_intake(_listing("Commerce Tower A", "https://example.com/listings/a", 120000))
    second = process_intake(_listing("Commerce Tower A", "https://example.com/listings/b"))
    assert second.outcome.value == "enriched"
    assert second.reason_code == "EVIDENCE_ONLY"
    assert second.filled_fields == []
    assert second.listing_evidence_id is not None
    (event,) = prov_store.events_for_run(second.run_id)
    assert _event_details(event)["mutation_status"] == "COMMITTED"


# 4. Duplicate listing URL (addressless repeat).


def test_duplicate_url(prov_store: _Store) -> None:
    first = process_intake(_listing("Lonely Hall", "https://example.com/listings/lonely", street=None))
    before = len(prov_store.queries)
    second = process_intake(_listing("Lonely Hall", "https://example.com/listings/lonely", street=None))
    assert second.outcome.value == "skipped_duplicate"
    assert second.reason_code == "DUPLICATE_URL"
    assert second.property_id == first.property_id
    (event,) = prov_store.events_for_run(second.run_id)
    details = _event_details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["considered_property_ids"] == [first.property_id]
    assert details["conflicting_property_ids"] == []
    new_queries = prov_store.queries[before:]
    assert not any(q.startswith("INSERT INTO properties") for q in new_queries)
    assert not any("INSERT INTO listing_evidence" in q for q in new_queries)
    assert sum("INSERT INTO agent_events" in q for q in new_queries) == 1


# 5. Duplicate normalized address.


def test_duplicate_address(prov_store: _Store) -> None:
    first = process_intake(_listing("Commerce Tower A", "https://example.com/listings/a", 120000))
    second = process_intake(_listing("Commerce Tower A", "https://example.com/listings/a", 120000))
    assert second.outcome.value == "skipped_duplicate"
    assert second.reason_code == "DUPLICATE_ADDRESS_RECORDED"
    (event,) = prov_store.events_for_run(second.run_id)
    details = _event_details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["result"]["property_id"] == first.property_id


# 6. Ambiguous address names every considered identity.


def test_ambiguous_address(prov_store: _Store) -> None:
    for pid, name in (("11111111-1111-1111-1111-111111111111", "Tower A"), ("22222222-2222-2222-2222-222222222222", "Tower B")):
        prov_store.properties[pid] = {
            "id": pid,
            "name": name,
            "street_address": "500 Commerce Street",
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75202",
        }
    result = process_intake(_listing("Tower C", "https://example.com/listings/c"))
    assert result.outcome.value == "needs_identity_review"
    assert result.reason_code == "AMBIGUOUS_ADDRESS"
    assert result.property_id == ""
    (event,) = prov_store.events_for_run(result.run_id)
    details = _event_details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert sorted(details["considered_property_ids"]) == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert sorted(details["conflicting_property_ids"]) == sorted(details["considered_property_ids"])
    assert event["property_id"] is None


# 7. Conflicting building name.


def test_name_conflict(prov_store: _Store) -> None:
    first = process_intake(_listing("Commerce Tower A", "https://example.com/listings/a", 120000))
    second = process_intake(_listing("Commerce Tower B", "https://example.com/listings/b", 240000))
    assert second.outcome.value == "needs_identity_review"
    assert second.reason_code == "NAME_CONFLICT"
    assert second.property_id == first.property_id
    (event,) = prov_store.events_for_run(second.run_id)
    details = _event_details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["considered_property_ids"] == [first.property_id]
    assert details["conflicting_property_ids"] == [first.property_id]
    assert prov_store.properties[first.property_id]["building_area_sqft_estimated"] == 120000


# 8. Listing URL conflict.


def test_url_conflict(prov_store: _Store) -> None:
    process_intake(_listing("Alpha Hall", "https://example.com/listings/shared", street="100 Main St"))
    result = process_intake(_listing("Beta Hall", "https://example.com/listings/shared", street="200 Main St"))
    assert result.outcome.value == "needs_identity_review"
    assert result.reason_code == "URL_CONFLICT"
    assert result.property_id == ""
    (event,) = prov_store.events_for_run(result.run_id)
    assert _event_details(event)["mutation_status"] == "NOT_ATTEMPTED"


# 9. Listing URL associated with multiple properties.


def test_url_multi_assoc(prov_store: _Store) -> None:
    first = process_intake(_listing("Alpha Hall", "https://example.com/listings/shared", street="100 Main St"))
    prov_store.evidence.append(
        {
            "id": "ev-seeded",
            "property_id": "33333333-3333-3333-3333-333333333333",
            "source_name": "Seeded",
            "source_url": "https://example.com/listings/shared",
        }
    )
    result = process_intake(_listing("Gamma Hall", "https://example.com/listings/shared", street="300 Main St"))
    assert result.outcome.value == "needs_identity_review"
    assert result.reason_code == "URL_MULTI_ASSOC"
    (event,) = prov_store.events_for_run(result.run_id)
    details = _event_details(event)
    assert sorted(details["considered_property_ids"]) == sorted(
        [first.property_id, "33333333-3333-3333-3333-333333333333"]
    )


# 10. URL/address identity disagreement.


def test_url_address_disagreement(prov_store: _Store) -> None:
    first = process_intake(_listing("Alpha Hall", "https://example.com/listings/alpha", street="100 Main St"))
    other_id = "44444444-4444-4444-4444-444444444444"
    prov_store.properties[other_id] = {
        "id": other_id,
        "name": "Other Hall",
        "street_address": "200 Main St",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
    }
    prov_store.evidence.append(
        {
            "id": "ev-other",
            "property_id": other_id,
            "source_name": "Seeded",
            "source_url": "https://example.com/listings/beta",
        }
    )
    result = process_intake(_listing("Alpha Hall", "https://example.com/listings/beta", street="100 Main St"))
    assert result.outcome.value == "needs_identity_review"
    assert result.reason_code == "URL_ADDRESS_DISAGREEMENT"
    assert result.property_id == ""
    (event,) = prov_store.events_for_run(result.run_id)
    details = _event_details(event)
    assert sorted(details["considered_property_ids"]) == sorted([first.property_id, other_id])
    assert sorted(details["conflicting_property_ids"]) == sorted([first.property_id, other_id])


# 11. Validation rejection.


def test_validation_rejection(monkeypatch: pytest.MonkeyPatch, prov_store: _Store) -> None:
    real_validate = intake.PropertyCreate.model_validate

    @classmethod
    def _boom(cls, *args: Any, **kwargs: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(intake.PropertyCreate, "model_validate", _boom)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake, "note_activity", lambda *a, **k: calls.append({"helper": "activity"}))
    monkeypatch.setattr(
        intake, "note_warning", lambda *a, **k: calls.append({"helper": "warning", **k}) or None
    )
    with pytest.raises(intake.IntakeError):
        process_intake(_listing("Bad Hall", "https://example.com/listings/bad"))
    assert len(prov_store.events) == 1
    details = _event_details(prov_store.events[0])
    assert details["outcome"] == "rejected"
    assert details["reason_code"] == "VALIDATION_FAILED"
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert [call["helper"] for call in calls] == ["warning"]
    monkeypatch.setattr(intake.PropertyCreate, "model_validate", real_validate)


# 12. Transaction rollback records an attempt failure.


def test_rollback_records_attempt_failed(prov_store: _Store) -> None:
    prov_store.fail_on = "INSERT INTO listing_evidence"
    with pytest.raises(DatabaseUnavailableError):
        process_intake(
            _listing(
                "Rollback Hall",
                "https://example.com/listings/rb",
                120000,
                distress_observations=[
                    {"indicator_type": "other", "description": "Long enough description here."}
                ],
            )
        )
    assert prov_store.properties == {}
    assert prov_store.evidence == []
    assert prov_store.distress == []
    assert len(prov_store.events) == 1
    details = _event_details(prov_store.events[0])
    assert details["outcome"] == "attempt_failed"
    assert details["reason_code"] == "DATABASE_FAILURE"
    assert details["mutation_status"] == "ROLLED_BACK"


# 13. Forced audit-insert failure rolls back the business mutation.


def test_audit_failure_rolls_back_business(monkeypatch: pytest.MonkeyPatch, prov_store: _Store) -> None:
    def _boom(conn: Any, event: Any) -> Any:
        raise DatabaseUnavailableError("audit down")

    monkeypatch.setattr(intake, "insert_event_in_transaction", _boom)
    with pytest.raises(DatabaseUnavailableError):
        process_intake(_listing("Audit Hall", "https://example.com/listings/audit", 120000))
    assert prov_store.properties == {}
    assert prov_store.evidence == []
    assert prov_store.events == []


# 14. Sequential same-identity intake yields consistent paired events.


def test_sequential_same_identity_events(prov_store: _Store) -> None:
    first = process_intake(_listing("Lonely Hall", "https://example.com/listings/lonely", street=None))
    second = process_intake(_listing("Lonely Hall", "https://example.com/listings/lonely", street=None))
    assert first.run_id != second.run_id
    (first_event,) = prov_store.events_for_run(first.run_id)
    (second_event,) = prov_store.events_for_run(second.run_id)
    assert _event_details(first_event)["outcome"] == "created"
    assert _event_details(second_event)["outcome"] == "skipped_duplicate"
    assert _event_details(second_event)["result"]["property_id"] == first.property_id


# 15. Exactly one permanent decision event per successful request.


def test_exactly_one_event_per_request(prov_store: _Store) -> None:
    created = process_intake(_listing("Hall A", "https://example.com/listings/1", 1000, street="1 Main St"))
    enriched = process_intake(_listing("Hall A", "https://example.com/listings/2", street="1 Main St"))
    duplicate = process_intake(_listing("Hall A", "https://example.com/listings/2", street="1 Main St"))
    review = process_intake(_listing("Hall B", "https://example.com/listings/3", street="1 Main St"))
    for result in (created, enriched, duplicate, review):
        assert len(prov_store.events_for_run(result.run_id)) == 1
    assert len(prov_store.events) == 4


# 16. Existing activity-event payload is byte-compatible.


def test_activity_event_unchanged(monkeypatch: pytest.MonkeyPatch, prov_store: _Store) -> None:
    calls: list[dict[str, Any]] = []

    def _tap(name: str):
        def _inner(run_id: str, event_type: str, summary: str, **kwargs: Any) -> None:
            calls.append({"helper": name, "run_id": run_id, "event_type": event_type, **kwargs})

        return _inner

    monkeypatch.setattr(intake, "note_activity", _tap("note_activity"))
    monkeypatch.setattr(intake, "note_warning", _tap("note_warning"))
    result = process_intake(_listing("Commerce Tower A", "https://example.com/listings/a", 120000))
    (accepted,) = [call for call in calls if call["event_type"] == "gabriel.intake.accepted"]
    assert accepted["run_id"] == result.run_id
    assert set(accepted["details"]) == {
        "outcome",
        "filled_fields",
        "listing_evidence_id",
        "distress_observations",
        "category",
        "listing_type",
        "source_name",
        "duration_ms",
    }
    assert accepted["evidence_refs"] == [
        {"kind": "listing_url", "ref": "https://example.com/listings/a"}
    ]


# 17. Observed versus inferred identity separation.


def test_observed_inferred_separation(prov_store: _Store) -> None:
    result = process_intake(
        _listing("  Commerce Tower A ", "https://example.com/listings/a", street="  500 Commerce Street  ")
    )
    (event,) = prov_store.events_for_run(result.run_id)
    details = _event_details(event)
    assert details["observed"]["name"] == "  Commerce Tower A "
    assert details["observed"]["street_address"] == "  500 Commerce Street  "
    assert details["inferred"]["name"] == _norm("  Commerce Tower A ")
    assert details["inferred"]["street_address"] == _norm("  500 Commerce Street  ")
    assert details["observed"]["street_address"] != details["inferred"]["street_address"]


# 18. No sensitive or unallowlisted request data.


def test_no_sensitive_data(prov_store: _Store) -> None:
    result = process_intake(
        _listing(
            "Commerce Tower A",
            "https://example.com/listings/a",
            120000,
            listing_notes="Private negotiator note",
            owner_advertised="Quiet Owner LLC",
            asking_price="5000000",
            occupancy_info="Half leased at signing",
        )
    )
    (event,) = prov_store.events_for_run(result.run_id)
    details = _event_details(event)
    assert set(details) == {
        "outcome",
        "reason_code",
        "message",
        "mutation_status",
        "observed",
        "inferred",
        "considered_property_ids",
        "conflicting_property_ids",
        "result",
        "duration_ms",
    }
    blob = json.dumps(details)
    for secret in ("Private negotiator note", "Quiet Owner LLC", "5000000", "Half leased at signing"):
        assert secret not in blob


def test_readonly_audit_failure_returns_error(monkeypatch: pytest.MonkeyPatch, prov_store: _Store) -> None:
    process_intake(_listing("Commerce Tower A", "https://example.com/listings/a", 120000))
    activity_calls: list[str] = []
    monkeypatch.setattr(intake, "note_activity", lambda *a, **k: activity_calls.append("accepted"))

    def _down(event: Any) -> None:
        raise DatabaseUnavailableError("event store down")

    monkeypatch.setattr(intake, "_persist_decision_dedicated", _down)
    with pytest.raises(DatabaseUnavailableError):
        process_intake(_listing("Commerce Tower A", "https://example.com/listings/a", 120000))
    assert activity_calls == []
    assert prov_store.properties and len(prov_store.properties) == 1


# --- Live PostgreSQL provenance (GROWUP_PG_INTEGRATION=1) ---

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _purge_scoped_reviews(cur, scope_sql: str, params: dict) -> None:
    """Delete test-owned identity-review rows for one event scope.

    A review case is removed only when its decision event, last event,
    and every occurrence event all belong to the scope; otherwise the
    cleanup fails loudly instead of touching foreign records.
    """
    cur.execute(
        "SELECT review_id FROM identity_reviews "
        f"WHERE decision_event_id IN ({scope_sql}) "
        f"OR last_event_id IN ({scope_sql}) "
        "OR review_id IN (SELECT review_id FROM identity_review_occurrences "
        f"WHERE event_id IN ({scope_sql}))",
        params,
    )
    doomed = [str(row["review_id"]) for row in cur.fetchall()]
    if not doomed:
        return
    check = dict(params)
    check["review_ids"] = doomed
    cur.execute(
        "SELECT 1 FROM identity_reviews "
        "WHERE review_id = ANY(%(review_ids)s) "
        f"AND (decision_event_id NOT IN ({scope_sql}) "
        f"OR last_event_id NOT IN ({scope_sql})) "
        "UNION ALL "
        "SELECT 1 FROM identity_review_occurrences "
        "WHERE review_id = ANY(%(review_ids)s) "
        f"AND event_id NOT IN ({scope_sql}) "
        "LIMIT 1",
        check,
    )
    if cur.fetchone() is not None:
        raise AssertionError(
            "identity review case references events outside this test's "
            "cleanup scope; refusing to delete"
        )
    cur.execute(
        "DELETE FROM identity_review_occurrences WHERE review_id = ANY(%(review_ids)s)",
        {"review_ids": doomed},
    )
    cur.execute(
        "DELETE FROM identity_reviews WHERE review_id = ANY(%(review_ids)s)",
        {"review_ids": doomed},
    )


def _live_cleanup(run_ids: list[str], streets: list[str]) -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            if run_ids:
                _purge_scoped_reviews(
                    cur,
                    "SELECT event_id FROM agent_events WHERE run_id = ANY(%(ids)s)",
                    {"ids": run_ids},
                )
                cur.execute(
                    "DELETE FROM agent_events WHERE run_id = ANY(%(ids)s)",
                    {"ids": run_ids},
                )
            for street in streets:
                cur.execute(
                    "DELETE FROM properties WHERE street_address = %(street)s",
                    {"street": street},
                )
        conn.commit()


def _live_events(run_id: str) -> list:
    from app.observability.store import query_events

    return query_events(run_id=run_id)


@needs_live_pg
def test_live_create_event_persisted() -> None:
    from uuid import uuid4

    street = f"500 Provenance Way {uuid4().hex}"
    url = f"https://example.com/listings/prov-{uuid4().hex}"
    run_ids: list[str] = []
    try:
        result = process_intake(_listing("Provenance Hall", url, 120000, street=street))
        run_ids.append(result.run_id)
        assert result.outcome.value == "created"
        (event,) = _live_events(result.run_id)
        assert event.event_type == "gabriel.intake.decision"
        assert event.retention_class.value == "permanent"
        assert event.details["reason_code"] == "NEW_IDENTITY"
        assert event.details["mutation_status"] == "COMMITTED"
        assert event.details["result"]["property_id"] == result.property_id
        assert event.details["observed"]["street_address"] == street
        assert str(event.property_id) == result.property_id
    finally:
        _live_cleanup(run_ids, [street])


@needs_live_pg
def test_live_review_event_persisted() -> None:
    from uuid import uuid4

    street = f"500 Provenance Way {uuid4().hex}"
    run_ids: list[str] = []
    try:
        first = process_intake(
            _listing("Provenance Hall A", f"https://example.com/listings/prov-a-{uuid4().hex}", 120000, street=street)
        )
        run_ids.append(first.run_id)
        second = process_intake(
            _listing("Provenance Hall B", f"https://example.com/listings/prov-b-{uuid4().hex}", 240000, street=street)
        )
        run_ids.append(second.run_id)
        assert second.reason_code == "NAME_CONFLICT"
        (event,) = _live_events(second.run_id)
        assert event.details["outcome"] == "needs_identity_review"
        assert event.details["mutation_status"] == "NOT_ATTEMPTED"
        assert event.details["considered_property_ids"] == [first.property_id]
    finally:
        _live_cleanup(run_ids, [street])


@needs_live_pg
def test_live_duplicate_event_persisted() -> None:
    from uuid import uuid4

    street = f"500 Provenance Way {uuid4().hex}"
    url = f"https://example.com/listings/prov-{uuid4().hex}"
    run_ids: list[str] = []
    try:
        first = process_intake(_listing("Provenance Hall", url, 120000, street=street))
        run_ids.append(first.run_id)
        second = process_intake(_listing("Provenance Hall", url, 120000, street=street))
        run_ids.append(second.run_id)
        assert second.reason_code == "DUPLICATE_ADDRESS_RECORDED"
        (event,) = _live_events(second.run_id)
        assert event.details["mutation_status"] == "NOT_ATTEMPTED"
        assert event.details["result"]["property_id"] == first.property_id
    finally:
        _live_cleanup(run_ids, [street])

