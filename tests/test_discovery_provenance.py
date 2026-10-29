"""Discovery decision-provenance tests (G2 Phase B).

Every successfully processed discovery candidate must produce exactly
one permanent ``gabriel.discovery.decision`` event; every completed run
attempts one permanent ``gabriel.discovery.run`` summary. Fake-driver
tests prove branching and transaction shape; live tests prove durable
PostgreSQL persistence and true registration races.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any
from uuid import uuid4

import pytest

from app.agents.gabriel import service as gabriel_service
from app.agents.gabriel.dedup import normalize_text as _norm
from app.agents.gabriel.schemas import DiscoveryRunRequest
from app.agents.gabriel.sources.base import RawListing
from app.db import DatabaseUnavailableError

_BBOX = {"south": 32.0, "west": -97.0, "north": 32.4, "east": -96.6}


class _Store:
    def __init__(self) -> None:
        self.properties: dict[str, dict[str, Any]] = {}
        self.observations: dict[tuple[str, str], dict[str, Any]] = {}
        self.evidence: list[dict[str, Any]] = []
        self.distress: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self.params: list[dict[str, Any]] = []
        self.conn_ids: list[int] = []
        self.fail_on: str | None = None
        self.fail_match_number: int = 1
        self._match_count = 0
        self._next_conn = 0

    def events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("run_id") == run_id]


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
            store._match_count += 1
            if store._match_count == store.fail_match_number:
                raise Exception("injected failure")
        self._rows = self._apply(query, dict(params or {}))

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def _apply(self, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        store = self._store
        upper = query.strip().upper()
        if "pg_advisory_xact_lock" in query:
            return [{"locked": True}]
        if "INSERT INTO agent_events" in query:
            store.events.append(dict(params))
            return []
        if "FROM listing_evidence" in query and upper.startswith("SELECT"):
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
        if "INSERT INTO source_observations" in query:
            key = (params["source"], params["external_id"])
            if key in store.observations:
                return []
            store.observations[key] = {
                "id": params["id"],
                "property_id": str(params["property_id"]),
            }
            return [dict(store.observations[key])]
        if "FROM source_observations" in query:
            row = store.observations.get((params["source"], params["external_id"]))
            return [dict(row)] if row is not None else []
        if "FROM properties" in query and "WHERE id" not in query and upper.startswith("SELECT"):
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
        if upper.startswith("INSERT INTO PROPERTIES"):
            row = dict(params)
            row.setdefault("created_at", "2026-10-20T00:00:00+00:00")
            row.setdefault("updated_at", "2026-10-20T00:00:00+00:00")
            store.properties[str(params["id"])] = row
            return [dict(row)]
        if "FROM properties WHERE id" in query:
            row = store.properties.get(str(params["id"]))
            if row is None:
                return []
            out = dict(row)
            if isinstance(out.get("field_evidence"), str):
                out["field_evidence"] = json.loads(out["field_evidence"])
            return [out]
        if upper.startswith("UPDATE PROPERTIES SET"):
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
        raise AssertionError(f"unexpected query in discovery repro: {query[:100]!r}")


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
                self._store.observations,
                self._store.evidence,
                self._store.distress,
                self._store.events,
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
            props, obs, ev, distress, events = copy.deepcopy(self._snapshot)
            self._store.properties = props
            self._store.observations = obs
            self._store.evidence = ev
            self._store.distress = distress
            self._store.events = events
            self._snapshot = None


@pytest.fixture
def disc_store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    """Route driver-level connections at an in-memory store with events."""
    store = _Store()
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "pw")
    monkeypatch.setattr("app.properties_db.get_connection", lambda config=None: _FakeConnection(store))
    monkeypatch.setattr("app.source_observations_db.get_connection", lambda config=None: _FakeConnection(store))
    monkeypatch.setattr("app.listings_db.get_connection", lambda config=None: _FakeConnection(store))
    monkeypatch.setattr(
        "app.agents.gabriel.service.get_connection", lambda config=None: _FakeConnection(store)
    )
    monkeypatch.setattr(
        "app.agents.gabriel.intake.get_connection", lambda config=None: _FakeConnection(store)
    )
    return store


class _StubSource:
    name = "stub"
    description = "test stub"
    docs_url = "https://example.invalid"
    requires_credentials = False

    def __init__(self, listings=None, error=None) -> None:
        self._listings = listings if listings is not None else []
        self._error = error

    def search(self, bbox, categories, limit, timeout_seconds):
        if self._error is not None:
            raise self._error
        return self._listings


def _listing(**overrides: Any) -> RawListing:
    base: dict[str, Any] = {
        "source": "overpass",
        "external_id": "way/1",
        "element_url": "https://www.openstreetmap.org/way/1",
        "name": "Discovery Hall",
        "street_address": "100 Main St",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
        "category_hint": "downtown_commercial",
        "latitude": 32.780,
        "longitude": -96.797,
        "retrieved_at": "2026-10-28T12:00:00+00:00",
    }
    base.update(overrides)
    return RawListing(**base)


def _request(**overrides: Any) -> DiscoveryRunRequest:
    body: dict[str, Any] = {"source": "stub", "bbox": dict(_BBOX), "limit": 10, "timeout_seconds": 15.0}
    body.update(overrides)
    return DiscoveryRunRequest.model_validate(body)


def _run(monkeypatch: pytest.MonkeyPatch, listings: list[RawListing]) -> Any:
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: _StubSource(listings))
    return gabriel_service.run_discovery(_request())


def _details(event: dict[str, Any]) -> dict[str, Any]:
    raw = event["details"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _positions(store: _Store, predicate) -> list[int]:
    return [i for i, query in enumerate(store.queries) if predicate(query)]


def _decisions(store: _Store, run_id: str) -> list[dict[str, Any]]:
    return [
        event for event in store.events_for_run(run_id)
        if event["event_type"] == "gabriel.discovery.decision"
    ]


def _run_events(store: _Store, run_id: str) -> list[dict[str, Any]]:
    return [
        event for event in store.events_for_run(run_id)
        if event["event_type"] == "gabriel.discovery.run"
    ]


# 1. Tracked creation: property, observation, event commit together.


def test_tracked_create_commits_atomically(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    result = _run(monkeypatch, [_listing()])
    assert result.counts.created == 1
    (event,) = _decisions(disc_store, result.run_id)
    assert event["retention_class"] == "permanent"
    details = _details(event)
    assert details["outcome"] == "created"
    assert details["reason_code"] == "NEW_IDENTITY"
    assert details["mutation_status"] == "COMMITTED"
    assert details["result"]["property_id"] == result.created_ids[0]
    assert details["result"]["observation_id"]
    assert event["property_id"] == result.created_ids[0]
    prop_pos = _positions(disc_store, lambda q: q.startswith("INSERT INTO properties"))
    obs_pos = _positions(disc_store, lambda q: "INSERT INTO source_observations" in q)
    audit_pos = _positions(disc_store, lambda q: "INSERT INTO agent_events" in q)
    commit_pos = _positions(disc_store, lambda q: q == "--commit--")
    assert len(prop_pos) == 1
    assert prop_pos[0] < obs_pos[0] < audit_pos[0] < commit_pos[0]
    conn_ids = disc_store.conn_ids
    assert conn_ids[prop_pos[0]] == conn_ids[obs_pos[0]] == conn_ids[audit_pos[0]] == conn_ids[commit_pos[0]]


# 2. Untracked creation: property and event commit together.


def test_untracked_create_commits_atomically(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    result = _run(monkeypatch, [_listing(source="mystery", external_id="m-1")])
    assert result.counts.created == 1
    (event,) = _decisions(disc_store, result.run_id)
    details = _details(event)
    assert details["outcome"] == "created"
    assert details["reason_code"] == "NEW_IDENTITY"
    assert details["mutation_status"] == "COMMITTED"
    assert details["result"]["observation_id"] is None
    prop_pos = _positions(disc_store, lambda q: q.startswith("INSERT INTO properties"))
    audit_pos = _positions(disc_store, lambda q: "INSERT INTO agent_events" in q)
    commit_pos = _positions(disc_store, lambda q: q == "--commit--")
    assert prop_pos[0] < audit_pos[0] < commit_pos[0]
    conn_ids = disc_store.conn_ids
    assert conn_ids[prop_pos[0]] == conn_ids[audit_pos[0]] == conn_ids[commit_pos[0]]


# 3. Duplicate source observation.


def test_duplicate_source_observation(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    first = _run(monkeypatch, [_listing()])
    assert first.counts.created == 1
    second = _run(monkeypatch, [_listing()])
    assert second.counts.created == 0
    assert second.counts.skipped_duplicate == 1
    assert second.skipped[0].reason_code == "ALREADY_RECORDED"
    (event,) = _decisions(disc_store, second.run_id)
    details = _details(event)
    assert details["outcome"] == "skipped_duplicate"
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["considered_property_ids"] == [first.created_ids[0]]
    assert details["conflicting_property_ids"] == []
    assert event["property_id"] == first.created_ids[0]


# 4. Duplicate address.


def test_duplicate_address(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    first = _run(monkeypatch, [_listing(external_id="way/1")])
    second = _run(monkeypatch, [_listing(source="mystery", external_id="m-9")])
    assert second.counts.skipped_duplicate == 1
    assert second.skipped[0].reason_code == "ADDRESS_MATCHED_STORED"
    (event,) = _decisions(disc_store, second.run_id)
    details = _details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["considered_property_ids"] == [first.created_ids[0]]
    assert event["property_id"] == first.created_ids[0]


# 5. Ambiguous address names every considered identity.


def test_ambiguous_address(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    disc_store.properties["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"] = {
        "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "street_address": "100 Main St",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
    }
    disc_store.properties["bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"] = {
        "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "street_address": "100 Main St",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
    }
    result = _run(monkeypatch, [_listing(source="mystery", external_id="m-9")])
    assert result.counts.skipped_duplicate == 1
    assert result.skipped[0].reason_code == "AMBIGUOUS_ADDRESS"
    (event,) = _decisions(disc_store, result.run_id)
    details = _details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert sorted(details["considered_property_ids"]) == [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    ]
    assert sorted(details["conflicting_property_ids"]) == sorted(details["considered_property_ids"])
    assert event["property_id"] is None


# 6. Conflicting recorded source.


def test_conflicting_recorded_source(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    first = _run(monkeypatch, [_listing()])
    assert first.counts.created == 1
    conflict = _listing(street_address="200 Other St")
    second = _run(monkeypatch, [conflict])
    assert second.counts.skipped_duplicate == 1
    assert second.skipped[0].reason_code == "RECORDED_ADDRESS_CONFLICT"
    (event,) = _decisions(disc_store, second.run_id)
    details = _details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["considered_property_ids"] == [first.created_ids[0]]
    assert details["conflicting_property_ids"] == [first.created_ids[0]]
    assert "recorded as property" in details["message"]
    assert "conflicts with incoming address" in details["message"]
    (summary,) = _run_events(disc_store, second.run_id)
    assert _details(summary)["counts"]["review_holds"] == 1


# 7. In-run duplicate records the earlier index.


def test_in_run_duplicate(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    result = _run(
        monkeypatch,
        [
            _listing(external_id="way/1"),
            _listing(external_id="way/2"),
        ],
    )
    assert result.counts.created == 1
    assert result.counts.skipped_duplicate == 1
    assert result.skipped[0].reason_code == "IN_RUN_DUPLICATE"
    events = _decisions(disc_store, result.run_id)
    assert len(events) == 2
    dup = next(event for event in events if _details(event)["reason_code"] == "IN_RUN_DUPLICATE")
    details = _details(dup)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["result"]["duplicate_of_index"] == 0
    assert details["considered_property_ids"] == []
    assert dup["property_id"] is None


# 9. Candidate validation failure.


def test_validation_failure(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    listing = _listing(city="X" * 500)
    result = _run(monkeypatch, [listing])
    assert result.counts.skipped_unusable == 1
    assert result.skipped[0].reason_code == "VALIDATION_FAILED"
    (event,) = _decisions(disc_store, result.run_id)
    details = _details(event)
    assert details["outcome"] == "skipped_unusable"
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["observed"]["city"] == "X" * 200
    (summary,) = _run_events(disc_store, result.run_id)
    assert _details(summary)["warnings"] == 1
    assert _details(summary)["errors"] == 0


# 10. Candidate normalization failure.


def test_normalization_failure(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    listing = _listing(name=None, street_address=None, latitude=None, longitude=None)
    result = _run(monkeypatch, [listing])
    assert result.counts.skipped_unusable == 1
    assert result.skipped[0].reason_code == "INSUFFICIENT_LOCATION"
    (event,) = _decisions(disc_store, result.run_id)
    details = _details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["observed"]["street_address"] is None
    assert details["observed"]["name"] is None


# 8 (fake half). Registration race loss rolls back, then records the winner.


def test_registration_race_loss(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    """A conflicting observation claim loses the race and records it durably."""
    winner_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    disc_store.properties[winner_id] = {
        "id": winner_id,
        "name": "Winner Hall",
        "street_address": "100 Main St",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
    }
    disc_store.observations[("osm", "way/7")] = {"id": "obs-winner", "property_id": winner_id}
    reads = {"count": 0}

    def _flaky_find(source: str, external_id: str) -> dict[str, Any] | None:
        # First read misses (concurrent winner lands after it); the claim
        # then conflicts and the re-read finds the winner.
        reads["count"] += 1
        if reads["count"] == 1:
            return None
        return disc_store.observations.get((source, external_id))

    monkeypatch.setattr(gabriel_service, "find_observation", _flaky_find)
    result = _run(monkeypatch, [_listing(external_id="way/7", street_address="700 Race St")])
    assert result.counts.created == 0
    assert result.counts.skipped_duplicate == 1
    assert result.skipped[0].reason_code == "REGISTRATION_RACE_LOST"
    assert winner_id in result.skipped[0].reason
    # Loser's speculative property rolled back; only the winner persists.
    assert list(disc_store.properties) == [winner_id]
    (event,) = _decisions(disc_store, result.run_id)
    details = _details(event)
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    assert details["considered_property_ids"] == [winner_id]
    assert details["conflicting_property_ids"] == []
    assert details["result"]["property_id"] == ""
    assert "rolled back" in details["message"]
    assert event["property_id"] == winner_id


# 11. Permanent audit-insert failure rolls back the candidate.


def test_audit_failure_rolls_back_candidate(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    disc_store.fail_on = "INSERT INTO agent_events"
    with pytest.raises(DatabaseUnavailableError):
        _run(monkeypatch, [_listing()])
    assert disc_store.properties == {}
    assert disc_store.observations == {}
    # No COMMITTED decision survives; only the best-effort abort record.
    assert len(disc_store.events) == 1
    assert _details(disc_store.events[0])["outcome"] == "run_aborted"


# 12. Candidate rollback preserves nothing for the failed candidate.


def test_candidate_rollback(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    disc_store.fail_on = "INSERT INTO properties"
    with pytest.raises(DatabaseUnavailableError):
        _run(monkeypatch, [_listing()])
    assert disc_store.properties == {}
    assert disc_store.observations == {}
    # Best-effort run-aborted record survives (dedicated transaction).
    aborts = _run_events(disc_store, disc_store.events[0]["run_id"]) if disc_store.events else []
    assert len(aborts) == 1
    assert _details(aborts[0])["outcome"] == "run_aborted"


# 13 + 15. Completed earlier candidates survive a later failure.


def test_partial_run_preservation(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    disc_store.fail_on = "INSERT INTO properties"
    disc_store.fail_match_number = 2
    with pytest.raises(DatabaseUnavailableError):
        _run(
            monkeypatch,
            [_listing(external_id="way/1"), _listing(external_id="way/2", street_address="200 Main St")],
        )
    assert len(disc_store.properties) == 1
    assert len(disc_store.observations) == 1
    run_ids = {event["run_id"] for event in disc_store.events}
    assert len(run_ids) == 1
    (run_id,) = run_ids
    decisions = _decisions(disc_store, run_id)
    assert len(decisions) == 1
    assert _details(decisions[0])["reason_code"] == "NEW_IDENTITY"
    aborts = _run_events(disc_store, run_id)
    assert len(aborts) == 1
    abort_details = _details(aborts[0])
    assert abort_details["outcome"] == "run_aborted"
    assert abort_details["counts"]["created"] == 1
    assert abort_details["failed_candidate"]["index"] == 1


# 13. Completed-run summary.


def test_run_completed_summary(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    result = _run(
        monkeypatch,
        [
            _listing(external_id="way/1"),
            _listing(external_id="way/2"),
            _listing(external_id="way/3", name=None, street_address=None, latitude=None, longitude=None),
        ],
    )
    assert result.counts.created == 1
    assert result.counts.skipped_duplicate == 1
    assert result.counts.skipped_unusable == 1
    (summary,) = _run_events(disc_store, result.run_id)
    assert summary["retention_class"] == "permanent"
    details = _details(summary)
    assert details["outcome"] == "run_completed"
    assert details["counts"]["returned"] == 3
    assert details["counts"]["created"] == 1
    assert details["counts"]["skipped_duplicate"] == 1
    assert details["counts"]["skipped_unusable"] == 1
    assert details["counts"]["review_holds"] == 0
    assert details["source"] == "stub"
    assert "duration_ms" in details


# 14. Mid-run abortion preserves the original exception.


def test_mid_run_abortion(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    from app.agents.gabriel.sources.base import SourceTimeout

    def _boom(name: str) -> Any:
        raise SourceTimeout("slow provider")

    monkeypatch.setattr(gabriel_service, "get_source", _boom)
    with pytest.raises(SourceTimeout):
        gabriel_service.run_discovery(_request())
    assert len(disc_store.events) == 1
    details = _details(disc_store.events[0])
    assert details["outcome"] == "run_aborted"
    assert details["reason_code"] == "SOURCE_FAILURE"
    assert details["counts"]["returned"] == 0


# 16. Stable run-ID correlation.


def test_run_id_correlation(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    result = _run(monkeypatch, [_listing(external_id="way/1"), _listing(external_id="way/2")])
    assert result.run_id
    for event in disc_store.events_for_run(result.run_id):
        assert event["run_id"] == result.run_id
    assert len(_decisions(disc_store, result.run_id)) == 2
    assert len(_run_events(disc_store, result.run_id)) == 1


# 17. Observed versus inferred fields.


def test_observed_inferred_separation(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    listing = _listing(
        name=None,
        street_address="  100 Main St  ",
        tags={"building": "yes", "secret_note": "do-not-store"},
    )
    result = _run(monkeypatch, [listing])
    (event,) = _decisions(disc_store, result.run_id)
    details = _details(event)
    assert details["observed"]["name"] is None
    assert details["observed"]["street_address"] == "  100 Main St  "
    assert details["observed"]["source"] == "overpass"
    assert details["observed"]["external_id"] == "way/1"
    assert details["inferred"]["canonical_source"] == "osm"
    assert details["inferred"]["normalized_address"]["street_address"] == _norm("  100 Main St  ")
    assert details["inferred"]["display_name_derived"] is True
    assert "tags" not in details["observed"]
    blob = json.dumps(details)
    assert "do-not-store" not in blob
    assert "secret_note" not in blob


# 18. Sensitive-field exclusion and payload allowlist.


def test_sensitive_field_exclusion(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    listing = _listing(tags={"owner": "Quiet LLC", "password": "hunter2", "price": "999"})
    result = _run(monkeypatch, [listing])
    (event,) = _decisions(disc_store, result.run_id)
    details = _details(event)
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
    assert set(details["observed"]) == {
        "source",
        "external_id",
        "element_url",
        "name",
        "street_address",
        "city",
        "state",
        "zip_code",
        "latitude",
        "longitude",
        "category_hint",
        "retrieved_at",
    }
    blob = json.dumps(details)
    for secret in ("Quiet LLC", "hunter2", "999", "owner", "password"):
        assert secret not in blob


# 19. Response compatibility.


def test_response_compatibility(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    from app.agents.gabriel.schemas import DiscoveryRunResult, SkippedCandidate

    result = _run(
        monkeypatch,
        [
            _listing(external_id="way/1"),
            _listing(external_id="way/2"),
            _listing(external_id="way/3", name=None, street_address=None, latitude=None, longitude=None),
        ],
    )
    assert result.counts.returned == 3
    assert result.counts.created == 1
    assert result.counts.skipped_duplicate == 1
    assert result.counts.skipped_unusable == 1
    assert len(result.created_ids) == 1
    reasons = [skip.reason for skip in result.skipped]
    assert any("within this run" in reason for reason in reasons)
    assert any("insufficient location" in reason for reason in reasons)
    assert all(skip.reason_code for skip in result.skipped)
    assert set(DiscoveryRunResult.model_fields) == {
        "source",
        "retrieved_at",
        "categories",
        "limit",
        "counts",
        "created_ids",
        "skipped",
        "run_id",
    }
    assert set(SkippedCandidate.model_fields) == {
        "external_id",
        "reason",
        "name",
        "street_address",
        "reason_code",
    }


# 20. Phase A intake coexistence on the shared event table.


def test_intake_discovery_coexistence(monkeypatch: pytest.MonkeyPatch, disc_store: _Store) -> None:
    from app.agents.gabriel.intake import ListingIntakeRequest, process_intake

    intake_result = process_intake(
        ListingIntakeRequest.model_validate(
            {
                "name": "Intake Hall",
                "street_address": "900 Intake Ave",
                "city": "Dallas",
                "state": "TX",
                "category": "downtown_commercial",
                "source_name": "Harborline Brokerage Records",
                "listing_url": "https://example.com/listings/intake-hall",
                "observed_at": "2026-10-20T12:00:00+00:00",
            }
        )
    )
    assert intake_result.outcome.value == "created"
    discovery_result = _run(monkeypatch, [_listing()])
    assert discovery_result.counts.created == 1
    assert intake_result.run_id != discovery_result.run_id
    assert len(disc_store.events_for_run(intake_result.run_id)) == 1
    intake_event = disc_store.events_for_run(intake_result.run_id)[0]
    assert intake_event["event_type"] == "gabriel.intake.decision"
    discovery_types = {event["event_type"] for event in disc_store.events_for_run(discovery_result.run_id)}
    assert discovery_types == {"gabriel.discovery.decision", "gabriel.discovery.run"}


# --- Live PostgreSQL provenance (GROWUP_PG_INTEGRATION=1) ---

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _live_cleanup(run_ids: list[str], streets: list[str], observations: list[tuple[str, str]]) -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            if run_ids:
                cur.execute("DELETE FROM agent_events WHERE run_id = ANY(%(ids)s)", {"ids": run_ids})
            for source, external_id in observations:
                cur.execute(
                    "DELETE FROM source_observations WHERE source = %(s)s AND external_id = %(e)s",
                    {"s": source, "e": external_id},
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
def test_live_tracked_create_event_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    token = uuid4().hex
    street = f"500 Discovery Way {token}"
    external_id = f"way/live-{token}"
    run_ids: list[str] = []
    try:
        result = _run(monkeypatch, [_listing(external_id=external_id, street_address=street)])
        run_ids.append(result.run_id)
        assert result.counts.created == 1
        events = _live_events(result.run_id)
        by_type = {}
        for event in events:
            by_type.setdefault(event.event_type, []).append(event)
        (decision,) = by_type["gabriel.discovery.decision"]
        assert decision.retention_class.value == "permanent"
        assert decision.details["reason_code"] == "NEW_IDENTITY"
        assert decision.details["mutation_status"] == "COMMITTED"
        assert decision.details["result"]["property_id"] == result.created_ids[0]
        (summary,) = by_type["gabriel.discovery.run"]
        assert summary.details["outcome"] == "run_completed"
        assert summary.details["counts"]["created"] == 1
    finally:
        _live_cleanup(run_ids, [street], [("osm", external_id)])


@needs_live_pg
def test_live_concurrent_registration_race(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    token = uuid4().hex
    street = f"500 Discovery Way {token}"
    external_id = f"way/race-{token}"
    run_ids: list[str] = []
    outcomes: list = []
    errors: list = []
    request = _request()
    monkeypatch.setattr(
        gabriel_service, "get_source", lambda name: _StubSource([_listing(external_id=external_id, street_address=street)])
    )

    def _attempt() -> None:
        try:
            outcomes.append(gabriel_service.run_discovery(request))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        assert sorted(o.counts.created for o in outcomes) == [0, 1]
        for outcome in outcomes:
            run_ids.append(outcome.run_id)
        codes = []
        for run_id in run_ids:
            (decision,) = [e for e in _live_events(run_id) if e.event_type == "gabriel.discovery.decision"]
            codes.append(decision.details["reason_code"])
            if decision.details["reason_code"] == "NEW_IDENTITY":
                assert decision.details["mutation_status"] == "COMMITTED"
            else:
                assert decision.details["reason_code"] == "REGISTRATION_RACE_LOST"
                assert decision.details["mutation_status"] == "NOT_ATTEMPTED"
        assert sorted(codes) == ["NEW_IDENTITY", "REGISTRATION_RACE_LOST"]
    finally:
        _live_cleanup(run_ids, [street], [("osm", external_id)])


@needs_live_pg
def test_live_skip_events_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    token = uuid4().hex
    street = f"500 Discovery Way {token}"
    external_id = f"way/skip-{token}"
    run_ids: list[str] = []
    try:
        first = _run(monkeypatch, [_listing(external_id=external_id, street_address=street)])
        run_ids.append(first.run_id)
        assert first.counts.created == 1
        second = _run(monkeypatch, [_listing(external_id=external_id, street_address=street)])
        run_ids.append(second.run_id)
        assert second.counts.skipped_duplicate == 1
        (decision,) = [e for e in _live_events(second.run_id) if e.event_type == "gabriel.discovery.decision"]
        assert decision.details["reason_code"] == "ALREADY_RECORDED"
        assert decision.details["considered_property_ids"] == [first.created_ids[0]]
    finally:
        _live_cleanup(run_ids, [street], [("osm", external_id)])


