"""Persistent source-observation tests (MVP-008).

Fake-driver unit tests plus gated live PostgreSQL integration
(GROWUP_PG_INTEGRATION=1 with a configured database). Live tests use
synthetic per-run-unique elements and clean up by property id.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest

import app.agents.gabriel.service as gabriel_service
from app.agents.gabriel.dedup import AddressMatch, AddressMatchStatus
from app.agents.gabriel.sources.base import BoundingBox, RawListing

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

_EVIDENCE_AT = "2026-10-27T00:00:00+00:00"
_BBOX_INPUT = {"south": 32.770, "west": -96.810, "north": 32.790, "east": -96.785}


# --- Canonical namespaces ---


def test_canonical_source_maps_osm_providers() -> None:
    from app.source_observations_db import canonical_source

    assert canonical_source("overpass") == "osm"
    assert canonical_source("osm_api") == "osm"


def test_canonical_source_rejects_unknown() -> None:
    from app.source_observations_db import canonical_source

    assert canonical_source("brave") is None
    assert canonical_source("Overpass") is None  # exact match, no fuzzy case folding
    assert canonical_source("") is None
    assert canonical_source(None) is None
    assert canonical_source(123) is None


# --- Module SQL shape (fake driver) ---


class _FakeCursor:
    def __init__(self, recorder: dict, rows: list) -> None:
        self._recorder = recorder
        self._rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, query, params=None) -> None:
        self._recorder["query"] = query
        self._recorder["params"] = params

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, recorder: dict, rows: list) -> None:
        self._recorder = recorder
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def cursor(self):
        return _FakeCursor(self._recorder, self._rows)

    def commit(self) -> None:
        pass


def _configured():
    from app.config import DatabaseConfig

    return DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw")


def test_find_observation_queries_canonical_link(monkeypatch) -> None:
    import app.source_observations_db as obs

    psycopg = pytest.importorskip("psycopg")
    recorder: dict = {}
    monkeypatch.setattr(
        psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, [{"id": "m", "property_id": "p"}])
    )
    assert obs.find_observation("osm", "way/1", config=_configured()) == {"id": "m", "property_id": "p"}
    assert "FROM source_observations" in recorder["query"]
    assert recorder["params"] == {"source": "osm", "external_id": "way/1"}
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: _FakeConnection(recorder, []))
    assert obs.find_observation("osm", "way/9", config=_configured()) is None


def test_record_observation_uses_conflict_safe_insert() -> None:
    import app.source_observations_db as obs

    recorder: dict = {}
    conn = _FakeConnection(recorder, [{"id": "m", "property_id": "p"}])
    assert obs.record_observation_in_transaction(conn, "osm", "way/1", "p") == {
        "id": "m",
        "property_id": "p",
    }
    assert "ON CONFLICT (source, external_id) DO NOTHING RETURNING" in recorder["query"]
    assert recorder["params"]["external_id"] == "way/1"
    assert recorder["params"]["property_id"] == "p"
    assert obs.record_observation_in_transaction(_FakeConnection(recorder, []), "osm", "way/1", "p") is None


# --- Migration structure ---


def test_migration_008_schema() -> None:
    sql_path = Path(__file__).resolve().parent.parent / "migrations" / "008_source_observations.sql"
    sql = sql_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS source_observations" in sql
    assert "source TEXT NOT NULL" in sql
    assert "external_id TEXT NOT NULL" in sql
    assert "property_id UUID NOT NULL REFERENCES properties(id) ON DELETE CASCADE" in sql
    assert "first_observed TIMESTAMPTZ NOT NULL DEFAULT now()" in sql
    assert "last_observed TIMESTAMPTZ NOT NULL DEFAULT now()" in sql
    assert "UNIQUE (source, external_id)" in sql
    assert "CREATE INDEX IF NOT EXISTS source_observations_property_idx" in sql
    assert sql.count("CREATE INDEX") == 1  # no redundant duplicate of the unique index
    assert "ALTER TABLE properties" not in sql
    assert "INSERT INTO" not in sql  # no backfill of historical provenance
    assert "UPDATE " not in sql and "DELETE FROM" not in sql


# --- Atomic creation semantics (scripted connection) ---


class _ScriptedCursor:
    def __init__(self, conn: _ScriptedConnection) -> None:
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, query, params=None) -> None:
        self._conn.statements.append(query)
        self._conn.executions += 1
        if self._conn.fail_on == self._conn.executions:
            raise RuntimeError("simulated driver failure")

    def fetchone(self):
        return self._conn.results.pop(0) if self._conn.results else None


class _ScriptedConnection:
    def __init__(self, results: list, fail_on: int | None = None) -> None:
        self.results = list(results)
        self.fail_on = fail_on
        self.statements: list = []
        self.executions = 0
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def cursor(self):
        return _ScriptedCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _property_payload() -> dict:
    return {"name": "X", "category": "other", "street_address": "500 Tx Way"}


def test_atomic_create_commits_property_and_link(monkeypatch) -> None:
    conn = _ScriptedConnection([{"id": "p-1"}, {"id": "m-1", "property_id": "p-1"}])
    monkeypatch.setattr(gabriel_service, "get_connection", lambda config=None: conn)
    row = gabriel_service._create_with_observation(_property_payload(), "osm", "way/1")
    assert row is not None and row["id"] == "p-1"
    assert (conn.commits, conn.rollbacks) == (1, 0)
    assert any("INSERT INTO properties" in s for s in conn.statements)
    assert any("INSERT INTO source_observations" in s for s in conn.statements)


def test_atomic_create_loser_rolls_back(monkeypatch) -> None:
    conn = _ScriptedConnection([{"id": "p-2"}, None])  # link claimed elsewhere
    monkeypatch.setattr(gabriel_service, "get_connection", lambda config=None: conn)
    assert gabriel_service._create_with_observation(_property_payload(), "osm", "way/1") is None
    assert conn.commits == 0 and conn.rollbacks == 1


def test_atomic_create_failure_rolls_back(monkeypatch) -> None:
    from app.db import DatabaseUnavailableError

    conn = _ScriptedConnection([{"id": "p-3"}, {"id": "m-3"}], fail_on=2)
    monkeypatch.setattr(gabriel_service, "get_connection", lambda config=None: conn)
    with pytest.raises(DatabaseUnavailableError):
        gabriel_service._create_with_observation(_property_payload(), "osm", "way/1")
    assert conn.commits == 0 and conn.rollbacks == 1


# --- Recorded-source discovery branches (mocked lookups, no DB) ---


class _StubSource:
    name = "stub"
    description = "test stub"
    docs_url = "https://example.invalid"
    requires_credentials = False

    def __init__(self, listings) -> None:
        self._listings = listings

    def search(self, bbox, categories, limit, timeout_seconds):
        return self._listings


def _listing(**overrides) -> RawListing:
    base = {
        "source": "overpass",
        "external_id": "way/9001",
        "element_url": "https://www.openstreetmap.org/way/9001",
        "name": "Observation Hall",
        "street_address": "500 Observe St",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
        "category_hint": "downtown_commercial",
        "latitude": 32.780,
        "longitude": -96.797,
        "retrieved_at": _EVIDENCE_AT,
    }
    base.update(overrides)
    return RawListing(**base)


def _run_request(**overrides):
    from app.agents.gabriel.schemas import DiscoveryRunRequest

    body = {"source": "stub", "bbox": dict(_BBOX_INPUT), "limit": 10, "timeout_seconds": 15.0}
    body.update(overrides)
    return DiscoveryRunRequest.model_validate(body)


def _configured_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_HOST", "localhost")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "db")
    monkeypatch.setenv("DATABASE_USER", "u")
    monkeypatch.setenv("DATABASE_PASSWORD", "pw")


def _guard_writes(monkeypatch) -> None:
    def _must_not_write(*args, **kwargs):
        raise AssertionError("recorded-source branch must not write")

    monkeypatch.setattr(gabriel_service, "_create_with_observation", _must_not_write)
    monkeypatch.setattr(gabriel_service, "_create_without_observation", _must_not_write)


_AMBIGUOUS = AddressMatch(AddressMatchStatus.AMBIGUOUS)


_PID_1 = "11111111-1111-4111-8111-111111111111"
_PID_2 = "22222222-2222-4222-8222-222222222222"


def _run_recorded(monkeypatch, listing, match, stored_street):
    """Run discovery with a recorded source link and mocked lookups (no DB)."""
    _configured_env(monkeypatch)
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: _StubSource([listing]))
    monkeypatch.setattr(
        gabriel_service, "find_observation",
        lambda source, external_id: {"id": "m-1", "property_id": _PID_1},
    )
    monkeypatch.setattr(
        gabriel_service, "get_property",
        lambda pid: {"id": _PID_1, "street_address": stored_street},
    )
    monkeypatch.setattr(gabriel_service, "match_existing_address", lambda payload: match)
    _guard_writes(monkeypatch)
    # Dedicated decision-event writes are stubbed here; durable event
    # persistence has its own test module.
    monkeypatch.setattr(gabriel_service, "_persist_decision_dedicated", lambda event: None)
    return gabriel_service.run_discovery(_run_request())


def test_recorded_consistent_reobservation_skips(monkeypatch) -> None:
    result = _run_recorded(
        monkeypatch, _listing(), AddressMatch(AddressMatchStatus.UNIQUE, _PID_1), "500 Observe St"
    )
    assert result.counts.created == 0
    assert result.skipped[0].reason == f"source already recorded as property {_PID_1}"


def test_recorded_different_property_requires_review(monkeypatch) -> None:
    result = _run_recorded(
        monkeypatch, _listing(), AddressMatch(AddressMatchStatus.UNIQUE, _PID_2), "500 Observe St"
    )
    assert result.counts.created == 0
    reason = result.skipped[0].reason
    assert f"previously recorded as property {_PID_1}" in reason
    assert f"different property {_PID_2}" in reason
    assert "held for review" in reason


def test_recorded_ambiguous_address_requires_review(monkeypatch) -> None:
    result = _run_recorded(monkeypatch, _listing(), _AMBIGUOUS, "500 Observe St")
    assert result.counts.created == 0
    reason = result.skipped[0].reason
    assert f"previously recorded as property {_PID_1}" in reason
    assert "multiple properties" in reason
    assert "held for review" in reason


def test_recorded_conflicting_address_requires_review(monkeypatch) -> None:
    result = _run_recorded(
        monkeypatch, _listing(), AddressMatch(AddressMatchStatus.NONE), "100 Other St"
    )
    assert result.counts.created == 0
    reason = result.skipped[0].reason
    assert f"previously recorded as property {_PID_1}" in reason
    assert "conflicts with incoming address" in reason
    assert "held for review" in reason


def test_recorded_address_gain_does_not_populate(monkeypatch) -> None:
    result = _run_recorded(
        monkeypatch, _listing(), AddressMatch(AddressMatchStatus.NONE), None
    )
    assert result.counts.created == 0
    reason = result.skipped[0].reason
    assert f"already recorded as property {_PID_1}" in reason
    assert "not populated without verification" in reason


def test_recorded_addressless_reobservation_skips(monkeypatch) -> None:
    result = _run_recorded(
        monkeypatch, _listing(street_address=None), AddressMatch(AddressMatchStatus.NONE), None
    )
    assert result.counts.created == 0
    assert result.skipped[0].reason == f"source already recorded as property {_PID_1}"


def test_lost_race_rereads_winner(monkeypatch) -> None:
    _configured_env(monkeypatch)
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: _StubSource([_listing()]))
    monkeypatch.setattr(
        gabriel_service, "match_existing_address",
        lambda payload: AddressMatch(AddressMatchStatus.NONE),
    )
    calls: list = []

    def _find(source, external_id):
        calls.append((source, external_id))
        if len(calls) == 1:
            return None  # first check: unrecorded
        return {"id": "m-winner", "property_id": "p-winner"}

    monkeypatch.setattr(gabriel_service, "find_observation", _find)
    monkeypatch.setattr(gabriel_service, "_create_with_observation", lambda payload, c, e, **_: None)
    monkeypatch.setattr(gabriel_service, "_persist_decision_dedicated", lambda event: None)
    result = gabriel_service.run_discovery(_run_request())
    assert result.counts.created == 0
    assert result.skipped[0].reason == "source already recorded as property p-winner"
    assert calls == [("osm", "way/9001"), ("osm", "way/9001")]


# --- Live PostgreSQL integration (GROWUP_PG_INTEGRATION=1) ---


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
                "to_regclass('public.source_observations') AS obs"
            )
            row = cur.fetchone()
    if row is None or row["props"] is None or row["obs"] is None:
        pytest.skip("properties/source_observations tables not present (migrations not applied)")


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
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM properties WHERE street_address = %(street)s",
                {"street": street},
            )
        conn.commit()


def _live_properties_by_street(street: str) -> list[dict]:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, street_address FROM properties WHERE street_address = %(street)s",
                {"street": street},
            )
            return [dict(row) for row in cur.fetchall()]


def _live_observation_rows(source: str, external_id: str) -> list[dict]:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, property_id FROM source_observations "
                "WHERE source = %(source)s AND external_id = %(external_id)s",
                {"source": source, "external_id": external_id},
            )
            return [dict(row) for row in cur.fetchall()]


def _live_seed_link(source: str, external_id: str, property_id: str) -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO source_observations (id, source, external_id, property_id) "
                "VALUES (%(id)s, %(source)s, %(external_id)s, %(property_id)s)",
                {
                    "id": str(uuid4()),
                    "source": source,
                    "external_id": external_id,
                    "property_id": str(property_id),
                },
            )
        conn.commit()


def _live_run(listings: list, run_ids: list[str] | None = None) -> object:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(gabriel_service, "get_source", lambda name: _StubSource(listings))
    try:
        result = gabriel_service.run_discovery(_run_request())
        if run_ids is not None:
            run_ids.append(result.run_id)
        return result
    finally:
        monkeypatch.undo()


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
    check["ids"] = doomed
    cur.execute(
        "SELECT 1 FROM identity_reviews "
        "WHERE review_id = ANY(%(ids)s) "
        f"AND (decision_event_id NOT IN ({scope_sql}) "
        f"OR last_event_id NOT IN ({scope_sql})) "
        "UNION ALL "
        "SELECT 1 FROM identity_review_occurrences "
        "WHERE review_id = ANY(%(ids)s) "
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
        "DELETE FROM identity_review_occurrences WHERE review_id = ANY(%(ids)s)",
        {"ids": doomed},
    )
    cur.execute(
        "DELETE FROM identity_reviews WHERE review_id = ANY(%(ids)s)",
        {"ids": doomed},
    )


def _live_cleanup_events(run_ids: list[str]) -> None:
    """Delete this execution's discovery decision/run events by run ID."""
    from app.properties_db import get_connection

    ids = [item for item in run_ids if item]
    if not ids:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            _purge_scoped_reviews(
                cur,
                "SELECT event_id FROM agent_events WHERE run_id = ANY(%(ids)s)",
                {"ids": ids},
            )
            cur.execute("DELETE FROM agent_events WHERE run_id = ANY(%(ids)s)", {"ids": ids})
        conn.commit()


def _live_cleanup_observation(source: str, external_id: str) -> None:
    """Delete properties linked to a run-unique source element (exact match).

    Fallback for addressless live tests, which have no street for
    street-scoped cleanup. Evidence cascades from the property delete.
    """
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM properties WHERE id IN ("
                "SELECT property_id FROM source_observations "
                "WHERE source = %(source)s AND external_id = %(external_id)s)",
                {"source": source, "external_id": external_id},
            )
        conn.commit()


def _live_property_row(property_id: str) -> dict | None:
    from uuid import UUID

    from app.properties_db import get_property

    return get_property(UUID(str(property_id)))


@needs_live_pg
def test_live_first_observation_creates_property_and_link() -> None:
    _live_config()
    _require_live_tables()
    token = uuid4().hex
    street = f"500 Live Observe Way {token}"
    ext = f"way/live-{token}"
    created: list[str] = []
    run_ids: list[str] = []
    try:
        result = _live_run([_listing(source="overpass", external_id=ext, street_address=street)], run_ids)
        assert result.counts.created == 1
        created.extend(result.created_ids)
        rows = _live_properties_by_street(street)
        assert len(rows) == 1
        links = _live_observation_rows("osm", ext)
        assert len(links) == 1
        assert str(links[0]["property_id"]) == created[0]
        # Repeat observation creates nothing further.
        repeat = _live_run([_listing(source="overpass", external_id=ext, street_address=street)], run_ids)
        assert repeat.counts.created == 0
        assert "already recorded" in repeat.skipped[0].reason
        assert len(_live_properties_by_street(street)) == 1
        assert len(_live_observation_rows("osm", ext)) == 1
    finally:
        _live_cleanup(created)
        _live_cleanup_street(street)
        _live_cleanup_events(run_ids)


@needs_live_pg
def test_live_providers_share_canonical_element() -> None:
    _live_config()
    _require_live_tables()
    token = uuid4().hex
    ext = f"way/live-canon-{token}"
    created: list[str] = []
    run_ids: list[str] = []
    try:
        first = _live_run([_listing(source="overpass", external_id=ext, street_address=None)], run_ids)
        assert first.counts.created == 1
        created.extend(first.created_ids)
        second = _live_run([_listing(source="osm_api", external_id=ext, street_address=None)], run_ids)
        assert second.counts.created == 0
        assert "already recorded" in second.skipped[0].reason
        assert len(_live_observation_rows("osm", ext)) == 1
        assert len(_live_observation_rows("overpass", ext)) == 0
        assert len(_live_observation_rows("osm_api", ext)) == 0
    finally:
        _live_cleanup(created)
        _live_cleanup_observation("osm", ext)
        _live_cleanup_events(run_ids)


@needs_live_pg
def test_live_distinct_elements_stay_distinct() -> None:
    _live_config()
    _require_live_tables()
    token = uuid4().hex
    ext_a = f"way/live-distinct-a-{token}"
    ext_b = f"way/live-distinct-b-{token}"
    created: list[str] = []
    run_ids: list[str] = []
    try:
        result = _live_run(
            [
                _listing(source="overpass", external_id=ext_a, street_address=None),
                _listing(source="overpass", external_id=ext_b, street_address=None),
            ],
            run_ids,
        )
        assert result.counts.created == 2
        created.extend(result.created_ids)
        assert len(_live_observation_rows("osm", ext_a)) == 1
        assert len(_live_observation_rows("osm", ext_b)) == 1
    finally:
        _live_cleanup(created)
        _live_cleanup_observation("osm", ext_a)
        _live_cleanup_observation("osm", ext_b)
        _live_cleanup_events(run_ids)


@needs_live_pg
def test_live_concurrent_observation_leaves_one_property() -> None:
    _live_config()
    _require_live_tables()
    token = uuid4().hex
    ext = f"way/live-race-{token}"
    barrier = threading.Barrier(2)
    outcomes: list = []
    errors: list = []
    patcher = pytest.MonkeyPatch()
    # One shared stub factory for both threads: patching is process-global
    # and must not be toggled per thread.
    patcher.setattr(
        gabriel_service, "get_source",
        lambda name: _StubSource([_listing(source="overpass", external_id=ext, street_address=None)]),
    )

    def _run_once() -> None:
        try:
            barrier.wait(timeout=30)
            outcomes.append(gabriel_service.run_discovery(_run_request()))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_run_once) for _ in range(2)]
    created: list[str] = []
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not [t for t in threads if t.is_alive()]
        assert errors == []
        for outcome in outcomes:
            created.extend(outcome.created_ids)
        assert sum(r.counts.created for r in outcomes) == 1
        assert sum(r.counts.skipped_duplicate for r in outcomes) == 1
        assert len(_live_observation_rows("osm", ext)) == 1
    finally:
        patcher.undo()
        _live_cleanup(created)
        _live_cleanup_observation("osm", ext)
        _live_cleanup_events([o.run_id for o in outcomes])


@needs_live_pg
def test_live_recorded_conflicting_address_requires_review() -> None:
    from app.properties_db import create_property

    _live_config()
    _require_live_tables()
    token = uuid4().hex
    street_a = f"500 Live Conflict Way {token}"
    street_b = f"600 Live Conflict Way {token}"
    ext = f"way/live-conflict-{token}"
    created: list[str] = []
    run_ids: list[str] = []
    try:
        seed = create_property(
            {
                "name": "Live Conflict Hall",
                "street_address": street_a,
                "city": "Dallas",
                "state": "TX",
                "zip_code": "75202",
                "category": "downtown_commercial",
                "provenance": "synthetic live observation-conflict seed record QA",
            }
        )
        created.append(str(seed["id"]))
        _live_seed_link("osm", ext, created[0])
        result = _live_run([_listing(source="overpass", external_id=ext, street_address=street_b)], run_ids)
        assert result.counts.created == 0
        reason = result.skipped[0].reason
        assert "previously recorded" in reason and "held for review" in reason
        assert len(_live_properties_by_street(street_b)) == 0
        assert len(_live_observation_rows("osm", ext)) == 1
    finally:
        _live_cleanup(created)
        _live_cleanup_street(street_a)
        _live_cleanup_street(street_b)
        _live_cleanup_events(run_ids)


@needs_live_pg
def test_live_recorded_ambiguous_address_requires_review() -> None:
    from app.properties_db import create_property

    _live_config()
    _require_live_tables()
    token = uuid4().hex
    street = f"500 Live Ambiguous Way {token}"
    ext = f"way/live-ambiguous-{token}"
    created: list[str] = []
    run_ids: list[str] = []
    try:
        for name in ("Live Ambiguous Hall A", "Live Ambiguous Hall B"):
            seed = create_property(
                {
                    "name": name,
                    "street_address": street,
                    "city": "Dallas",
                    "state": "TX",
                    "zip_code": "75202",
                    "category": "downtown_commercial",
                    "provenance": "synthetic live observation-ambiguity seed record QA",
                }
            )
            created.append(str(seed["id"]))
        _live_seed_link("osm", ext, created[0])
        result = _live_run([_listing(source="overpass", external_id=ext, street_address=street)], run_ids)
        assert result.counts.created == 0
        reason = result.skipped[0].reason
        assert "multiple properties" in reason and "held for review" in reason
        assert len(_live_properties_by_street(street)) == 2
    finally:
        _live_cleanup(created)
        _live_cleanup_street(street)
        _live_cleanup_events(run_ids)


@needs_live_pg
def test_live_recorded_address_gain_does_not_modify() -> None:
    from app.properties_db import create_property

    _live_config()
    _require_live_tables()
    token = uuid4().hex
    street = f"500 Live Gain Way {token}"
    ext = f"way/live-gain-{token}"
    created: list[str] = []
    run_ids: list[str] = []
    try:
        seed = create_property(
            {
                "name": "Live Gain Hall",
                "street_address": None,
                "city": "Dallas",
                "state": "TX",
                "zip_code": "75202",
                "category": "downtown_commercial",
                "provenance": "synthetic live observation-gain seed record QA",
            }
        )
        created.append(str(seed["id"]))
        _live_seed_link("osm", ext, created[0])
        before = _live_property_row(created[0])
        result = _live_run([_listing(source="overpass", external_id=ext, street_address=street)], run_ids)
        assert result.counts.created == 0
        assert "not populated without verification" in result.skipped[0].reason
        assert _live_property_row(created[0]) == before
        assert len(_live_properties_by_street(street)) == 0
    finally:
        _live_cleanup(created)
        _live_cleanup_street(street)
        _live_cleanup_events(run_ids)


@needs_live_pg
def test_live_address_match_records_no_link() -> None:
    from app.properties_db import create_property

    _live_config()
    _require_live_tables()
    token = uuid4().hex
    street = f"500 Live Nosync Way {token}"
    ext = f"way/live-nosync-{token}"
    created: list[str] = []
    run_ids: list[str] = []
    try:
        seed = create_property(
            {
                "name": "Live Nosync Hall",
                "street_address": street,
                "city": "Dallas",
                "state": "TX",
                "zip_code": "75202",
                "category": "downtown_commercial",
                "provenance": "synthetic live no-link-on-match seed record QA",
            }
        )
        created.append(str(seed["id"]))
        result = _live_run([_listing(source="overpass", external_id=ext, street_address=street)], run_ids)
        assert result.counts.created == 0
        assert "already stored" in result.skipped[0].reason
        assert len(_live_properties_by_street(street)) == 1
        assert _live_observation_rows("osm", ext) == []
    finally:
        _live_cleanup(created)
        _live_cleanup_street(street)
        _live_cleanup_events(run_ids)
