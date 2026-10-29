"""Atomic intake and advisory-lock tests.

Unit coverage for lock-key derivation and deterministic multi-lock
ordering runs locally. Live PostgreSQL integration
(GROWUP_PG_INTEGRATION=1 with a configured database) verifies committed
row counts, concurrent same-key intake, and rollback under injected
faults. Live tests use synthetic per-run-unique URLs and addresses and
clean up by property id with exact-match fallbacks.
"""

from __future__ import annotations

import os
import threading
from uuid import UUID, uuid4

import pytest

from app.agents.gabriel.dedup import intake_address_lock_key, intake_url_lock_key
from app.agents.gabriel.intake import ListingIntakeRequest, process_intake
from app.db import acquire_intake_locks

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


# --- Lock keys ---


def test_url_lock_key_is_verbatim() -> None:
    url = "https://example.com/listings/ABC-123?x=1"
    assert intake_url_lock_key(url) == f"gabriel-intake-url:{url}"
    assert intake_url_lock_key(url) != intake_url_lock_key(url.lower())
    assert intake_url_lock_key(url) != intake_url_lock_key(url + "/")


def test_address_lock_key_uses_symmetric_normalization() -> None:
    base = intake_address_lock_key("500 Commerce St.", "Dallas", "TX", "75202")
    assert base == intake_address_lock_key("500 commerce st", "dallas", "tx", "75202")
    assert base == intake_address_lock_key("500  Commerce   St.", " Dallas ", "TX", "75202")
    assert base.startswith("gabriel-intake-addr:")


def test_address_lock_key_preserves_material_distinctions() -> None:
    base = ("500 Commerce St", "Dallas", "TX", "75202")
    key = intake_address_lock_key(*base)
    assert key != intake_address_lock_key("500 Commerce Street", "Dallas", "TX", "75202")
    assert key != intake_address_lock_key("500 Commerce St", "Dallas", "TX", None or "")
    assert key != intake_address_lock_key("500 Commerce St Suite 100", "Dallas", "TX", "75202")
    assert key != intake_address_lock_key("N 500 Commerce St", "Dallas", "TX", "75202")
    assert key != intake_address_lock_key("500 Commerce St", "Irving", "TX", "75202")


def test_address_lock_key_component_boundaries() -> None:
    # Length-prefixing keeps ("a", "b|c") distinct from ("a|b", "c").
    left = intake_address_lock_key("a", "b|c")
    right = intake_address_lock_key("a|b", "c")
    assert left != right


def test_intake_lock_keys_per_request() -> None:
    from app.agents.gabriel.intake import _intake_lock_keys

    addressed = _intake_lock_keys(
        {"street_address": "500 Commerce St", "city": "Dallas"}, "https://example.com/x"
    )
    assert len(addressed) == 2
    assert addressed[0].startswith("gabriel-intake-url:")
    addressless = _intake_lock_keys({"street_address": None}, "https://example.com/x")
    assert addressless == ["gabriel-intake-url:https://example.com/x"]
    assert _intake_lock_keys({"street_address": None}, "") == []


# --- Deterministic multi-lock ordering ---


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple] = []

    def execute(self, query, params=None) -> None:
        self.statements.append((query, dict(params or {})))


def test_acquire_orders_and_dedupes_keys() -> None:
    cur = _RecordingCursor()
    acquire_intake_locks(cur, ["k-b", "k-a", "k-b", "k-c"])
    assert [params["key"] for _, params in cur.statements] == ["k-a", "k-b", "k-c"]
    for query, _ in cur.statements:
        assert "pg_advisory_xact_lock(hashtext(" in query


def test_acquire_no_keys_no_statements() -> None:
    cur = _RecordingCursor()
    acquire_intake_locks(cur, [])
    assert cur.statements == []


# --- Live PostgreSQL integration (GROWUP_PG_INTEGRATION=1) ---


def _live_config():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _live_listing(name, url, area=None, street="500 Live Atomic Way", city="Dallas",
                  zip_code="75202", distress=None) -> ListingIntakeRequest:
    body = {
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
    if distress is not None:
        body["distress_observations"] = distress
    return ListingIntakeRequest.model_validate(body)


_DISTRESS = [{"indicator_type": "other", "description": "Long enough description here."}]


def _live_counts(urls: list[str], streets: list[str]) -> tuple[int, int, int]:
    from app.properties_db import get_connection

    props = ev = dist = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            if streets:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM properties WHERE street_address = ANY(%(s)s)",
                    {"s": streets},
                )
                props = int(cur.fetchone()["n"])
            elif urls:
                # Addressless properties have no street to count by: count
                # the distinct properties linked to the exact test URLs.
                cur.execute(
                    "SELECT COUNT(DISTINCT property_id) AS n FROM listing_evidence "
                    "WHERE source_url = ANY(%(u)s)",
                    {"u": urls},
                )
                props = int(cur.fetchone()["n"])
            if urls:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM listing_evidence WHERE source_url = ANY(%(u)s)",
                    {"u": urls},
                )
                ev = int(cur.fetchone()["n"])
                cur.execute(
                    "SELECT COUNT(*) AS n FROM distress_observations WHERE source_url = ANY(%(u)s)",
                    {"u": urls},
                )
                dist = int(cur.fetchone()["n"])
    return props, ev, dist


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


def _live_cleanup_urls_streets(urls: list[str], streets: list[str]) -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            if urls:
                _purge_scoped_reviews(
                    cur,
                    "SELECT event_id FROM agent_events WHERE event_type = 'gabriel.intake.decision' "
                    "AND details->'observed'->>'listing_url' = ANY(%(u)s)",
                    {"u": urls},
                )
                cur.execute(
                    "DELETE FROM properties WHERE id IN ("
                    "SELECT property_id FROM listing_evidence WHERE source_url = ANY(%(u)s))",
                    {"u": urls},
                )
                cur.execute(
                    "DELETE FROM agent_events WHERE event_type = 'gabriel.intake.decision' "
                    "AND details->'observed'->>'listing_url' = ANY(%(u)s)",
                    {"u": urls},
                )
            for street in streets:
                _purge_scoped_reviews(
                    cur,
                    "SELECT event_id FROM agent_events WHERE event_type = 'gabriel.intake.decision' "
                    "AND details->'observed'->>'street_address' = %(street)s",
                    {"street": street},
                )
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


def _live_evidence_by_url(url: str) -> list[dict]:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, property_id, source_url FROM listing_evidence "
                "WHERE source_url = %(url)s",
                {"url": url},
            )
            return [dict(row) for row in cur.fetchall()]


def _live_property_by_street(street: str) -> dict | None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM properties WHERE street_address = %(street)s LIMIT 2",
                {"street": street},
            )
            rows = cur.fetchall()
    assert len(rows) <= 1
    return dict(rows[0]) if rows else None


@needs_live_pg
def test_live_atomic_creation_commits_all_rows() -> None:
    _live_config()
    token = uuid4().hex
    street = f"500 Live Atomic Way {token}"
    url = f"https://example.com/listings/live-atomic-{token}"
    try:
        result = process_intake(_live_listing("Live Atomic Hall", url, 120000, street, distress=_DISTRESS))
        assert result.outcome.value == "created"
        assert _live_counts([url], [street]) == (1, 1, 1)
        row = _live_property_by_street(street)
        assert row is not None and float(row["building_area_sqft_estimated"]) == 120000
    finally:
        _live_cleanup_urls_streets([url], [street])


@needs_live_pg
def test_live_atomic_enrichment_commits_all_rows() -> None:
    _live_config()
    token = uuid4().hex
    street = f"500 Live Enrich Way {token}"
    url_a = f"https://example.com/listings/live-enrich-a-{token}"
    url_b = f"https://example.com/listings/live-enrich-b-{token}"
    try:
        first = process_intake(_live_listing("Live Enrich Hall", url_a, None, street))
        assert first.outcome.value == "created"
        second = process_intake(
            _live_listing("Live Enrich Hall", url_b, 240000, street, distress=_DISTRESS)
        )
        assert second.outcome.value == "enriched"
        assert second.filled_fields == ["building_area_sqft_estimated"]
        assert _live_counts([url_a, url_b], [street]) == (1, 2, 1)
        row = _live_property_by_street(street)
        assert row is not None and float(row["building_area_sqft_estimated"]) == 240000
    finally:
        _live_cleanup_urls_streets([url_a, url_b], [street])


def _run_threaded(pairs: list[tuple]) -> tuple[list, list]:
    outcomes: list = []
    errors: list = []
    barrier = threading.Barrier(len(pairs))

    def _run_once(body) -> None:
        try:
            barrier.wait(timeout=30)
            outcomes.append(process_intake(body))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=_run_once, args=(body,)) for _, body in pairs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not [t for t in threads if t.is_alive()]
    return outcomes, errors


@needs_live_pg
def test_live_concurrent_same_address_intake() -> None:
    _live_config()
    token = uuid4().hex
    street = f"500 Live Race Way {token}"
    url_a = f"https://example.com/listings/live-race-a-{token}"
    url_b = f"https://example.com/listings/live-race-b-{token}"
    try:
        bodies = [
            ("a", _live_listing("Live Race Hall A", url_a, 120000, street)),
            ("b", _live_listing("Live Race Hall B", url_b, 240000, street)),
        ]
        outcomes, errors = _run_threaded(bodies)
        assert errors == []
        assert sorted(r.outcome.value for r in outcomes) == ["created", "needs_identity_review"]
        assert _live_counts([url_a, url_b], [street]) == (1, 1, 0)
    finally:
        _live_cleanup_urls_streets([url_a, url_b], [street])


@needs_live_pg
def test_live_concurrent_same_url_addressless_intake() -> None:
    _live_config()
    token = uuid4().hex
    url = f"https://example.com/listings/live-url-race-{token}"
    try:
        bodies = [
            ("a", _live_listing("Live URL Race Hall", url, None, street=None, distress=_DISTRESS)),
            ("b", _live_listing("Live URL Race Hall", url, None, street=None, distress=_DISTRESS)),
        ]
        outcomes, errors = _run_threaded(bodies)
        assert errors == []
        assert sorted(r.outcome.value for r in outcomes) == ["created", "skipped_duplicate"]
        assert _live_counts([url], []) == (1, 1, 1)
        created_ids = [r.property_id for r in outcomes if r.outcome.value == "created"]
        assert len(created_ids) == 1
        rows = _live_evidence_by_url(url)
        assert len(rows) == 1
        assert str(rows[0]["property_id"]) == created_ids[0]
    finally:
        _live_cleanup_urls_streets([url], [])


@needs_live_pg
def test_live_concurrent_addressed_and_addressless_share_url() -> None:
    _live_config()
    token = uuid4().hex
    street = f"500 Live Mixed Way {token}"
    url = f"https://example.com/listings/live-mixed-{token}"
    try:
        bodies = [
            ("a", _live_listing("Live Mixed Hall", url, 120000, street)),
            ("b", _live_listing("Live Mixed Hall", url, None, street=None)),
        ]
        outcomes, errors = _run_threaded(bodies)
        assert errors == []
        # Both requests lock the shared URL key: exactly one creates.
        assert sorted(r.outcome.value for r in outcomes) == ["created", "skipped_duplicate"]
        assert _live_counts([url], []) == (1, 1, 0)
        created_ids = [r.property_id for r in outcomes if r.outcome.value == "created"]
        assert len(created_ids) == 1
        rows = _live_evidence_by_url(url)
        assert len(rows) == 1
        assert str(rows[0]["property_id"]) == created_ids[0]
        # Either request may win. Request a carries area 120000 and the
        # street; request b carries neither, so the stored area identifies
        # the winner independently of completion order.
        from app.properties_db import get_property

        winner = get_property(UUID(created_ids[0]))
        assert winner is not None
        if winner["building_area_sqft_estimated"] is not None:
            assert float(winner["building_area_sqft_estimated"]) == 120000
            assert winner["street_address"] == street
        else:
            assert winner["street_address"] is None
    finally:
        _live_cleanup_urls_streets([url], [street])


@needs_live_pg
def test_live_repeated_addressed_intake() -> None:
    _live_config()
    token = uuid4().hex
    street = f"500 Live Repeat Way {token}"
    url = f"https://example.com/listings/live-repeat-{token}"
    try:
        first = process_intake(_live_listing("Live Repeat Hall", url, 120000, street))
        assert first.outcome.value == "created"
        second = process_intake(_live_listing("Live Repeat Hall", url, 120000, street))
        assert second.outcome.value == "skipped_duplicate"
        assert second.property_id == first.property_id
        assert _live_counts([url], [street]) == (1, 1, 0)
    finally:
        _live_cleanup_urls_streets([url], [street])


@needs_live_pg
def test_live_create_rollback_on_evidence_failure(monkeypatch) -> None:
    import app.agents.gabriel.intake as intake

    _live_config()
    token = uuid4().hex
    street = f"500 Live Rollback Way {token}"
    url = f"https://example.com/listings/live-rollback-{token}"

    def _fail(*args, **kwargs):
        raise RuntimeError("injected evidence failure")

    monkeypatch.setattr(intake, "create_listing_evidence_in_transaction", _fail)
    try:
        from app.db import DatabaseUnavailableError

        with pytest.raises(DatabaseUnavailableError):
            process_intake(_live_listing("Live Rollback Hall", url, 120000, street, distress=_DISTRESS))
        assert _live_counts([url], [street]) == (0, 0, 0)
    finally:
        _live_cleanup_urls_streets([url], [street])


@needs_live_pg
def test_live_enrich_rollback_on_distress_failure(monkeypatch) -> None:
    import app.agents.gabriel.intake as intake

    _live_config()
    token = uuid4().hex
    street = f"500 Live EnrichRb Way {token}"
    url_a = f"https://example.com/listings/live-enrichrb-a-{token}"
    url_b = f"https://example.com/listings/live-enrichrb-b-{token}"
    try:
        first = process_intake(_live_listing("Live EnrichRb Hall", url_a, None, street))
        assert first.outcome.value == "created"

        def _fail(*args, **kwargs):
            raise RuntimeError("injected distress failure")

        monkeypatch.setattr(intake, "create_distress_observations_in_transaction", _fail)
        from app.db import DatabaseUnavailableError

        with pytest.raises(DatabaseUnavailableError):
            process_intake(
                _live_listing("Live EnrichRb Hall", url_b, 240000, street, distress=_DISTRESS)
            )
        row = _live_property_by_street(street)
        assert row is not None and row["building_area_sqft_estimated"] is None
        assert _live_counts([url_a, url_b], [street]) == (1, 1, 0)
    finally:
        _live_cleanup_urls_streets([url_a, url_b], [street])
