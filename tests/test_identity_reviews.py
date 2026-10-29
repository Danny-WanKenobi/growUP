"""IR-3A: durable operational identity-review queue (tests-first).

Local tests exercise the migration-file contract, the review-eligibility
gate, exact-match deduplication keys, and fail-closed validation without
any database. Live tests (``GROWUP_PG_INTEGRATION=1``) verify the real
PostgreSQL schema, constraints, transactions, trigger wiring, occurrence
integrity, and concurrency on isolated synthetic rows.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_009 = REPO_ROOT / "migrations" / "009_identity_reviews.sql"

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


# --- B1 (local): migration-file contract ------------------------------------


def _migration_sql() -> str:
    assert MIGRATION_009.exists(), "migrations/009_identity_reviews.sql is missing"
    return MIGRATION_009.read_text(encoding="utf-8")


def test_migration_009_creates_both_review_tables() -> None:
    sql = _migration_sql()
    assert "CREATE TABLE IF NOT EXISTS identity_reviews" in sql
    assert "CREATE TABLE IF NOT EXISTS identity_review_occurrences" in sql


def test_migration_009_event_links_are_text_fks() -> None:
    sql = _migration_sql()
    assert "REFERENCES agent_events(event_id)" in sql
    assert "decision_event_id TEXT NOT NULL" in sql
    assert "event_id TEXT NOT NULL" in sql


def test_migration_009_enforces_pending_uniqueness_and_source_rules() -> None:
    sql = _migration_sql()
    assert "UNIQUE" in sql
    assert "WHERE" in sql and "PENDING" in sql
    assert "conflicting_property_ids" in sql
    assert "candidate_key" in sql
    assert "CHECK" in sql


def test_migration_009_touches_no_existing_table() -> None:
    sql = _migration_sql().upper()
    assert "DROP TABLE" not in sql
    for guarded in ("AGENT_EVENTS", "PROPERTIES", "LISTING_EVIDENCE", "SOURCE_OBSERVATIONS"):
        assert f"ALTER TABLE {guarded}" not in sql
        assert f"ALTER TABLE IF EXISTS {guarded}" not in sql


# --- Fixture builders (real production event builders, no I/O) --------------


def _intake_body(**overrides):  # type: ignore[no-untyped-def]
    from app.agents.gabriel.intake import ListingIntakeRequest

    body = {
        "name": "Commerce Tower B",
        "street_address": "500 Commerce Street",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
        "category": "downtown_commercial",
        "source_name": "Harborline Brokerage Records",
        "listing_url": "https://example.com/listings/review-b",
        "observed_at": "2026-10-20T12:00:00+00:00",
    }
    body.update(overrides)
    return ListingIntakeRequest.model_validate(body)


def _intake_event(reason_code, conflicting, **overrides):  # type: ignore[no-untyped-def]
    """One synthetic permanent intake decision event via the real builder."""
    from app.agents.gabriel.intake import _DecisionContext, _decision_event

    body = overrides.pop("body", _intake_body())
    ctx = _DecisionContext(
        body=body,
        conn=None,
        run_id=overrides.pop("run_id", f"run-{uuid4().hex}"),
        started=0.0,
        source_url=str(body.listing_url),
    )
    return _decision_event(
        ctx,
        overrides.pop("outcome", "needs_identity_review"),
        reason_code,
        overrides.pop("message", f"review: {reason_code}"),
        overrides.pop("mutation_status", "NOT_ATTEMPTED"),
        overrides.pop("considered", ()),
        tuple(conflicting),
        None,
    )


def _discovery_item(**overrides):  # type: ignore[no-untyped-def]
    from app.agents.gabriel.sources.base import RawListing

    params = {
        "source": "osm_api",
        "external_id": "way/1001",
        "element_url": "https://example.com/osm/way-1001",
        "name": "Commerce Tower",
        "street_address": "500 Commerce Street",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75202",
    }
    params.update(overrides)
    return RawListing(**params)


def _discovery_event(reason_code, conflicting, **overrides):  # type: ignore[no-untyped-def]
    """One synthetic permanent discovery decision event via the real builder."""
    from app.agents.gabriel.service import _build_candidate_event

    return _build_candidate_event(
        run_id=overrides.pop("run_id", f"run-{uuid4().hex}"),
        index=0,
        started=0.0,
        item=overrides.pop("item", _discovery_item()),
        payload=overrides.pop("payload", {"name": "Commerce Tower"}),
        dedup_key_value=None,
        outcome=overrides.pop("outcome", "skipped_duplicate"),
        reason_code=reason_code,
        message=overrides.pop("message", f"review: {reason_code}"),
        mutation_status=overrides.pop("mutation_status", "NOT_ATTEMPTED"),
        considered=overrides.pop("considered", ()),
        conflicting=tuple(conflicting),
    )


# --- B2 (local): all nine operational review triggers ------------------------


def _pids(count: int = 2) -> tuple[str, ...]:
    return tuple(str(uuid4()) for _ in range(count))


def test_review_reason_constants_match_production_branches() -> None:
    from app.agents.gabriel import intake as intake_mod
    from app.agents.gabriel import service as service_mod
    from app import identity_reviews_db as reviews

    assert reviews.INTAKE_REVIEW_REASONS == frozenset(
        {
            intake_mod.REASON_URL_MULTI_ASSOC,
            intake_mod.REASON_URL_CONFLICT,
            intake_mod.REASON_AMBIGUOUS_ADDRESS,
            intake_mod.REASON_NAME_CONFLICT,
            intake_mod.REASON_URL_ADDRESS_DISAGREEMENT,
        }
    )
    assert reviews.DISCOVERY_REVIEW_REASONS == frozenset(service_mod.REVIEW_CODES)
    assert len(reviews.INTAKE_REVIEW_REASONS) == 5
    assert len(reviews.DISCOVERY_REVIEW_REASONS) == 4


@pytest.mark.parametrize(
    "reason_code",
    [
        "URL_MULTI_ASSOC",
        "URL_CONFLICT",
        "AMBIGUOUS_ADDRESS",
        "NAME_CONFLICT",
        "URL_ADDRESS_DISAGREEMENT",
    ],
)
def test_intake_review_triggers_are_queue_eligible(reason_code: str) -> None:
    from app import identity_reviews_db as reviews

    pids = _pids(2)
    event = _intake_event(reason_code, pids)
    ctx = reviews.review_context_from_event(event)
    assert ctx is not None
    assert ctx["source"] == "intake"
    assert ctx["reason_code"] == reason_code
    assert ctx["decision_event_id"] == event.event_id
    assert ctx["run_id"] == event.run_id
    assert ctx["conflicting_property_ids"] == sorted(pids)
    assert ctx["listing_url"] == "https://example.com/listings/review-b"
    assert ctx["observed_identity"]["name"] == "Commerce Tower B"
    assert ctx["observed_identity"]["street_address"] == "500 Commerce Street"
    assert ctx["dedup_key"]  # opaque but present and non-empty


@pytest.mark.parametrize(
    "reason_code",
    [
        "RECORDED_MULTI_MATCH",
        "RECORDED_DIFFERENT_PROPERTY",
        "RECORDED_ADDRESS_CONFLICT",
        "AMBIGUOUS_ADDRESS",
    ],
)
def test_discovery_review_triggers_are_queue_eligible(reason_code: str) -> None:
    from app import identity_reviews_db as reviews

    pids = _pids(2)
    event = _discovery_event(reason_code, pids)
    ctx = reviews.review_context_from_event(event)
    assert ctx is not None
    assert ctx["source"] == "discovery"
    assert ctx["reason_code"] == reason_code
    assert ctx["decision_event_id"] == event.event_id
    assert ctx["run_id"] == event.run_id
    assert ctx["conflicting_property_ids"] == sorted(pids)
    assert ctx["provider"] == "osm_api"
    assert ctx["external_id"] == "way/1001"
    assert ctx["listing_url"] == "https://example.com/osm/way-1001"
    assert ctx["observed_identity"]["street_address"] == "500 Commerce Street"
    assert ctx["dedup_key"]


def test_non_review_decisions_are_not_queue_eligible() -> None:
    from app import identity_reviews_db as reviews
    from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity

    pids = _pids(1)
    cases = [
        _intake_event("NEW_IDENTITY", (), outcome="created", mutation_status="COMMITTED"),
        _intake_event("ENRICHED_MATCH", pids, outcome="enriched", mutation_status="COMMITTED"),
        _intake_event("DUPLICATE_URL", pids, outcome="skipped_duplicate"),
        _intake_event("VALIDATION_FAILED", (), outcome="rejected"),
        _intake_event("DATABASE_FAILURE", (), outcome="attempt_failed"),
        _discovery_event("NEW_IDENTITY", (), outcome="created", mutation_status="COMMITTED"),
        _discovery_event("IN_RUN_DUPLICATE", ()),
        _discovery_event("VALIDATION_FAILED", (), outcome="skipped_unusable"),
        _discovery_event("REGISTRATION_RACE_LOST", pids),
        AgentEvent(
            agent=AgentId.GABRIEL,
            run_id=f"run-{uuid4().hex}",
            event_type="gabriel.research.candidate_decision",
            summary="research verdict",
            severity=Severity.INFO,
            retention_class=RetentionClass.PERMANENT,
            details={"decision": "RESEARCH_REQUIRED", "reason_codes": ["CONFLICTING_NAMES"]},
        ),
    ]
    for event in cases:
        assert reviews.review_context_from_event(event) is None


def test_review_context_rejects_empty_conflicting_set() -> None:
    from app import identity_reviews_db as reviews

    event = _intake_event("NAME_CONFLICT", ())
    with pytest.raises(reviews.IdentityReviewError):
        reviews.review_context_from_event(event)


def test_review_context_rejects_invalid_property_uuid() -> None:
    from app import identity_reviews_db as reviews

    event = _intake_event("NAME_CONFLICT", ("not-a-uuid",))
    with pytest.raises(reviews.IdentityReviewError):
        reviews.review_context_from_event(event)


def test_review_context_rejects_mutation_claim_on_review() -> None:
    from app import identity_reviews_db as reviews

    event = _intake_event("NAME_CONFLICT", _pids(1), mutation_status="COMMITTED")
    with pytest.raises(reviews.IdentityReviewError):
        reviews.review_context_from_event(event)


def test_review_context_rejects_non_permanent_event() -> None:
    from app import identity_reviews_db as reviews
    from app.observability.events import RetentionClass

    event = _discovery_event("AMBIGUOUS_ADDRESS", _pids(2))
    degraded = event.model_copy(update={"retention_class": RetentionClass.ACTIVITY})
    with pytest.raises(reviews.IdentityReviewError):
        reviews.review_context_from_event(degraded)


def test_record_occurrence_ignores_ineligible_event_without_io() -> None:
    from unittest.mock import MagicMock

    from app import identity_reviews_db as reviews

    event = _intake_event("NEW_IDENTITY", (), outcome="created", mutation_status="COMMITTED")
    conn = MagicMock()
    assert reviews.record_review_occurrence(conn, event) is None
    conn.cursor.assert_not_called()
    conn.execute.assert_not_called()


# --- B3 (local): exact-match deduplication ------------------------------------


def _key(event) -> str:  # type: ignore[no-untyped-def]
    from app import identity_reviews_db as reviews

    ctx = reviews.review_context_from_event(event)
    assert ctx is not None
    return str(ctx["dedup_key"])


def test_dedup_key_is_deterministic_canonical_json() -> None:
    pids = _pids(2)
    event = _intake_event("NAME_CONFLICT", pids)
    first, second = _key(event), _key(event)
    assert first == second
    parsed = json.loads(first)
    assert parsed["v"] == 1
    assert parsed["source"] == "intake"
    assert parsed["reason"] == "NAME_CONFLICT"
    assert parsed["pids"] == sorted(pids)


def test_identical_repeat_produces_identical_key() -> None:
    pids = _pids(2)
    key_a = _key(_intake_event("AMBIGUOUS_ADDRESS", pids))
    key_b = _key(_intake_event("AMBIGUOUS_ADDRESS", tuple(reversed(pids))))
    assert key_a == key_b


def test_changed_conflicting_set_produces_separate_case() -> None:
    first = _pids(2)
    assert _key(_intake_event("AMBIGUOUS_ADDRESS", first)) != _key(
        _intake_event("AMBIGUOUS_ADDRESS", (first[0], str(uuid4())))
    )


def test_different_reason_produces_separate_case() -> None:
    pids = _pids(2)
    assert _key(_intake_event("NAME_CONFLICT", pids)) != _key(
        _intake_event("URL_CONFLICT", pids)
    )


def test_shared_property_alone_never_establishes_case_equivalence() -> None:
    pids = _pids(1)
    base = _key(_intake_event("NAME_CONFLICT", pids))
    renamed = _key(
        _intake_event("NAME_CONFLICT", pids, body=_intake_body(name="Harborline Annex"))
    )
    relocated = _key(
        _intake_event("NAME_CONFLICT", pids, body=_intake_body(street_address="700 Main Street"))
    )
    relinked = _key(
        _intake_event(
            "NAME_CONFLICT", pids, body=_intake_body(listing_url="https://example.com/other")
        )
    )
    assert len({base, renamed, relocated, relinked}) == 4


def test_equivalent_spellings_share_one_case() -> None:
    pids = _pids(2)
    base = _key(_intake_event("AMBIGUOUS_ADDRESS", pids))
    variant = _key(
        _intake_event(
            "AMBIGUOUS_ADDRESS",
            pids,
            body=_intake_body(street_address="  500   commerce street. "),
        )
    )
    assert base == variant


def test_discovery_key_tracks_source_external_and_address() -> None:
    pids = _pids(2)
    base = _key(_discovery_event("RECORDED_ADDRESS_CONFLICT", pids))
    other_ext = _key(
        _discovery_event(
            "RECORDED_ADDRESS_CONFLICT", pids, item=_discovery_item(external_id="way/9999")
        )
    )
    other_addr = _key(
        _discovery_event(
            "RECORDED_ADDRESS_CONFLICT", pids, item=_discovery_item(street_address="700 Main St")
        )
    )
    assert len({base, other_ext, other_addr}) == 3


def test_discovery_key_uses_canonical_osm_source() -> None:
    pids = _pids(1)
    via_api = _key(_discovery_event("AMBIGUOUS_ADDRESS", pids))
    via_overpass = _key(
        _discovery_event(
            "AMBIGUOUS_ADDRESS", pids, item=_discovery_item(source="overpass")
        )
    )
    assert via_api == via_overpass


def test_missing_optional_identity_fields_are_keyed_safely() -> None:
    pids = _pids(1)
    nameless_a = _key(
        _discovery_event(
            "AMBIGUOUS_ADDRESS", pids, item=_discovery_item(name=None, element_url=None)
        )
    )
    nameless_b = _key(
        _discovery_event(
            "AMBIGUOUS_ADDRESS", pids, item=_discovery_item(name=None, element_url=None)
        )
    )
    named = _key(_discovery_event("AMBIGUOUS_ADDRESS", pids))
    assert nameless_a == nameless_b
    assert nameless_a != named


# --- B1/B6 (local): driver-error translation ------------------------------------


class _FailingCursor:
    """Cursor that fails exactly like a PostgreSQL driver error, without a server."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __enter__(self) -> _FailingCursor:
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def execute(self, *args: object, **kwargs: object) -> None:
        raise self._error


class _FailingConn:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.commits = 0

    def cursor(self) -> _FailingCursor:
        return _FailingCursor(self._error)

    def commit(self) -> None:
        self.commits += 1


@pytest.mark.parametrize(
    "driver_error_cls",
    ["OperationalError", "ForeignKeyViolation", "UniqueViolation"],
)
def test_queue_driver_failure_becomes_database_unavailable(driver_error_cls: str) -> None:
    import psycopg
    import psycopg.errors

    from app import identity_reviews_db as reviews
    from app.db import DatabaseUnavailableError

    namespace = psycopg if driver_error_cls == "OperationalError" else psycopg.errors
    driver_error = getattr(namespace, driver_error_cls)(f"synthetic {driver_error_cls}")
    event = _intake_event("NAME_CONFLICT", _pids(1))
    conn = _FailingConn(driver_error)
    with pytest.raises(DatabaseUnavailableError) as exc_info:
        reviews.record_review_occurrence(conn, event)
    assert exc_info.value.__cause__ is driver_error
    assert conn.commits == 0


def test_identity_review_error_is_not_wrapped() -> None:
    from unittest.mock import MagicMock

    from app import identity_reviews_db as reviews

    event = _intake_event("NAME_CONFLICT", ())
    conn = MagicMock()
    with pytest.raises(reviews.IdentityReviewError):
        reviews.record_review_occurrence(conn, event)
    conn.cursor.assert_not_called()


def test_existing_database_errors_pass_through_unwrapped() -> None:
    from app import identity_reviews_db as reviews
    from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError

    event = _intake_event("NAME_CONFLICT", _pids(1))
    for original in (
        DatabaseUnavailableError("already translated"),
        DatabaseNotConfiguredError("not configured"),
    ):
        conn = _FailingConn(original)
        with pytest.raises(type(original)) as exc_info:
            reviews.record_review_occurrence(conn, event)
        assert exc_info.value is original


def test_programming_errors_are_not_disguised_as_outages() -> None:
    from app import identity_reviews_db as reviews

    event = _intake_event("NAME_CONFLICT", _pids(1))
    boom = RuntimeError("synthetic programming fault")
    with pytest.raises(RuntimeError) as exc_info:
        reviews.record_review_occurrence(_FailingConn(boom), event)
    assert exc_info.value is boom


# --- Live PostgreSQL integration (GROWUP_PG_INTEGRATION=1) -------------------
#
# Same gate and conventions as the existing live-DB tests: ambient
# DATABASE_* config, synthetic rows only, persisted-row verification, and
# try/finally cleanup. Queue rows are removed before their parent events
# (foreign keys restrict deletion while children exist).


def _live_config():  # type: ignore[no-untyped-def]
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _require_review_tables() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.identity_reviews') AS reviews, "
                "to_regclass('public.identity_review_occurrences') AS occ"
            )
            row = cur.fetchone()
    if row is None or row["reviews"] is None or row["occ"] is None:
        pytest.skip("identity_reviews tables not present (migration 009 not applied)")


def _live_insert_parent_event(cur, event_id: str) -> None:  # type: ignore[no-untyped-def]
    cur.execute(
        "INSERT INTO agent_events (event_id, occurred_at, agent, run_id, event_type, "
        "severity, summary, details, evidence_refs, retention_class) "
        "VALUES (%(eid)s, NOW(), 'gabriel', %(run)s, 'gabriel.intake.decision', "
        "'INFO', 'synthetic review parent', '{}', '[]', 'permanent')",
        {"eid": event_id, "run": f"run-{uuid4().hex}"},
    )


def _live_cleanup_queue(review_ids: list[str], event_ids: list[str]) -> None:
    from app.properties_db import get_connection

    reviews = [str(item) for item in review_ids if item]
    events = [str(item) for item in event_ids if item]
    if not reviews and not events:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            if reviews:
                cur.execute(
                    "DELETE FROM identity_review_occurrences WHERE review_id = ANY(%(ids)s)",
                    {"ids": reviews},
                )
                cur.execute(
                    "DELETE FROM identity_reviews WHERE review_id = ANY(%(ids)s)",
                    {"ids": reviews},
                )
            if events:
                cur.execute(
                    "DELETE FROM agent_events WHERE event_id = ANY(%(ids)s)", {"ids": events}
                )
        conn.commit()


def _live_review_row(review_id: str):  # type: ignore[no-untyped-def]
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM identity_reviews WHERE review_id = %(rid)s", {"rid": review_id}
            )
            return cur.fetchone()


def _live_occurrences(review_id: str):  # type: ignore[no-untyped-def]
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT event_id FROM identity_review_occurrences "
                "WHERE review_id = %(rid)s ORDER BY created_at",
                {"rid": review_id},
            )
            return [row["event_id"] for row in cur.fetchall()]


# --- B1 (live): schema and constraints ----------------------------------------


@needs_live_pg
def test_live_schema_tables_exist() -> None:
    from app.properties_db import get_connection

    _live_config()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.identity_reviews') AS reviews, "
                "to_regclass('public.identity_review_occurrences') AS occ"
            )
            row = cur.fetchone()
    assert row is not None
    assert row["reviews"] == "identity_reviews", "migration 009 not applied"
    assert row["occ"] == "identity_review_occurrences", "migration 009 not applied"


@needs_live_pg
def test_live_schema_column_types_and_keys() -> None:
    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, udt_name, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name IN ('identity_reviews', 'identity_review_occurrences')"
            )
            cols = {(r["table_name"], r["column_name"]): r for r in cur.fetchall()}
            cur.execute(
                "SELECT conrelid::regclass AS tbl, contype FROM pg_constraint "
                "WHERE conrelid IN ('identity_reviews'::regclass, "
                "'identity_review_occurrences'::regclass) AND contype = 'p'"
            )
            pks = {str(r["tbl"]) for r in cur.fetchall()}
    assert cols[("identity_reviews", "review_id")]["udt_name"] == "uuid"
    assert cols[("identity_reviews", "dedup_key")]["udt_name"] == "text"
    assert cols[("identity_reviews", "decision_event_id")]["udt_name"] == "text"
    assert cols[("identity_reviews", "conflicting_property_ids")]["udt_name"] == "_uuid"
    assert cols[("identity_reviews", "observed_identity")]["udt_name"] == "jsonb"
    assert cols[("identity_reviews", "candidate_key")]["is_nullable"] == "YES"
    assert cols[("identity_review_occurrences", "occurrence_id")]["udt_name"] == "uuid"
    assert cols[("identity_review_occurrences", "event_id")]["udt_name"] == "text"
    assert pks == {"identity_reviews", "identity_review_occurrences"}


@needs_live_pg
def test_live_operational_review_requires_conflicting_property() -> None:
    import psycopg

    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    eid = f"evt-{uuid4().hex}"
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _live_insert_parent_event(cur, eid)
            conn.commit()
        with get_connection() as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.IntegrityError):
                    cur.execute(
                        "INSERT INTO identity_reviews (review_id, dedup_key, source, "
                        "reason_code, observed_identity, conflicting_property_ids, "
                        "decision_event_id, run_id, last_event_id) "
                        "VALUES (%(rid)s, %(key)s, 'intake', 'NAME_CONFLICT', '{}', "
                        "'{}', %(eid)s, 'run-x', %(eid)s)",
                        {"rid": str(uuid4()), "key": f"key-{uuid4().hex}", "eid": eid},
                    )
            conn.rollback()
    finally:
        _live_cleanup_queue([], [eid])


@needs_live_pg
def test_live_research_reviews_use_candidate_key_not_property_ids() -> None:
    import psycopg

    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    eid = f"evt-{uuid4().hex}"
    created: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _live_insert_parent_event(cur, eid)
            conn.commit()
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Research with fabricated operational property ids is rejected.
                with pytest.raises(psycopg.IntegrityError):
                    cur.execute(
                        "INSERT INTO identity_reviews (review_id, dedup_key, source, "
                        "reason_code, observed_identity, conflicting_property_ids, "
                        "candidate_key, decision_event_id, run_id, last_event_id) "
                        "VALUES (%(rid)s, %(key)s, 'research', 'CONFLICTING_NAMES', '{}', "
                        "ARRAY[%(pid)s]::uuid[], 'addr:abc', %(eid)s, 'run-x', %(eid)s)",
                        {
                            "rid": str(uuid4()),
                            "key": f"key-{uuid4().hex}",
                            "pid": str(uuid4()),
                            "eid": eid,
                        },
                    )
                conn.rollback()
                # Research without a candidate reference is rejected.
                with get_connection() as conn2, conn2.cursor() as cur2:
                    with pytest.raises(psycopg.IntegrityError):
                        cur2.execute(
                            "INSERT INTO identity_reviews (review_id, dedup_key, source, "
                            "reason_code, observed_identity, decision_event_id, run_id, "
                            "last_event_id) VALUES (%(rid)s, %(key)s, 'research', "
                            "'CONFLICTING_NAMES', '{}', %(eid)s, 'run-x', %(eid)s)",
                            {"rid": str(uuid4()), "key": f"key-{uuid4().hex}", "eid": eid},
                        )
                conn2.rollback()
        # A well-formed research row is structurally accepted (IR-3C writes none).
        with get_connection() as conn:
            with conn.cursor() as cur:
                rid = str(uuid4())
                cur.execute(
                    "INSERT INTO identity_reviews (review_id, dedup_key, source, "
                    "reason_code, observed_identity, candidate_key, decision_event_id, "
                    "run_id, last_event_id) VALUES (%(rid)s, %(key)s, 'research', "
                    "'CONFLICTING_NAMES', '{}', 'addr:abc', %(eid)s, 'run-x', %(eid)s)",
                    {"rid": rid, "key": f"key-{uuid4().hex}", "eid": eid},
                )
            conn.commit()
            created.append(rid)
    finally:
        _live_cleanup_queue(created, [eid])


@needs_live_pg
def test_live_pending_uniqueness_allows_resolved_history() -> None:
    import psycopg

    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    key = f"key-{uuid4().hex}"
    pid = str(uuid4())
    eids = [f"evt-{uuid4().hex}", f"evt-{uuid4().hex}", f"evt-{uuid4().hex}"]
    created: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for eid in eids:
                    _live_insert_parent_event(cur, eid)
            conn.commit()
        with get_connection() as conn:
            with conn.cursor() as cur:
                first = str(uuid4())
                cur.execute(
                    "INSERT INTO identity_reviews (review_id, dedup_key, source, reason_code, "
                    "observed_identity, conflicting_property_ids, decision_event_id, run_id, "
                    "last_event_id) VALUES (%(rid)s, %(key)s, 'intake', 'NAME_CONFLICT', "
                    "'{}', ARRAY[%(pid)s]::uuid[], %(eid)s, 'run-x', %(eid)s)",
                    {"rid": first, "key": key, "pid": pid, "eid": eids[0]},
                )
            conn.commit()
            created.append(first)
            # A second PENDING row on the same key is rejected.
            with get_connection() as conn2, conn2.cursor() as cur2:
                with pytest.raises(psycopg.IntegrityError):
                    cur2.execute(
                        "INSERT INTO identity_reviews (review_id, dedup_key, source, "
                        "reason_code, observed_identity, conflicting_property_ids, "
                        "decision_event_id, run_id, last_event_id) "
                        "VALUES (%(rid)s, %(key)s, 'intake', 'NAME_CONFLICT', '{}', "
                        "ARRAY[%(pid)s]::uuid[], %(eid)s, 'run-x', %(eid)s)",
                        {"rid": str(uuid4()), "key": key, "pid": pid, "eid": eids[1]},
                    )
            conn2.rollback()
            # Resolving the first row frees the key while preserving history.
            with get_connection() as conn3, conn3.cursor() as cur3:
                cur3.execute(
                    "UPDATE identity_reviews SET status = 'RESOLVED' WHERE review_id = %(rid)s",
                    {"rid": first},
                )
                second = str(uuid4())
                cur3.execute(
                    "INSERT INTO identity_reviews (review_id, dedup_key, source, reason_code, "
                    "observed_identity, conflicting_property_ids, decision_event_id, run_id, "
                    "last_event_id) VALUES (%(rid)s, %(key)s, 'intake', 'NAME_CONFLICT', "
                    "'{}', ARRAY[%(pid)s]::uuid[], %(eid)s, 'run-x', %(eid)s)",
                    {"rid": second, "key": key, "pid": pid, "eid": eids[2]},
                )
            conn3.commit()
            created.append(second)
    finally:
        _live_cleanup_queue(created, eids)


@needs_live_pg
def test_live_occurrence_links_are_unique_and_referential() -> None:
    import psycopg

    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    eid = f"evt-{uuid4().hex}"
    created: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _live_insert_parent_event(cur, eid)
                rid = str(uuid4())
                cur.execute(
                    "INSERT INTO identity_reviews (review_id, dedup_key, source, reason_code, "
                    "observed_identity, conflicting_property_ids, decision_event_id, run_id, "
                    "last_event_id) VALUES (%(rid)s, %(key)s, 'discovery', "
                    "'AMBIGUOUS_ADDRESS', '{}', ARRAY[%(pid)s]::uuid[], %(eid)s, 'run-x', "
                    "%(eid)s)",
                    {
                        "rid": rid,
                        "key": f"key-{uuid4().hex}",
                        "pid": str(uuid4()),
                        "eid": eid,
                    },
                )
                cur.execute(
                    "INSERT INTO identity_review_occurrences (occurrence_id, review_id, "
                    "event_id) VALUES (%(oid)s, %(rid)s, %(eid)s)",
                    {"oid": str(uuid4()), "rid": rid, "eid": eid},
                )
                # The same event cannot be counted twice, even on another case.
                with pytest.raises(psycopg.IntegrityError):
                    cur.execute(
                        "INSERT INTO identity_review_occurrences (occurrence_id, review_id, "
                        "event_id) VALUES (%(oid)s, %(rid)s, %(eid)s)",
                        {"oid": str(uuid4()), "rid": rid, "eid": eid},
                    )
            conn.rollback()
        # An occurrence without a real permanent event is rejected.
        with get_connection() as conn:
            with conn.cursor() as cur:
                with pytest.raises(psycopg.IntegrityError):
                    cur.execute(
                        "INSERT INTO identity_review_occurrences (occurrence_id, review_id, "
                        "event_id) VALUES (%(oid)s, %(rid)s, %(eid)s)",
                        {
                            "oid": str(uuid4()),
                            "rid": str(uuid4()),
                            "eid": f"evt-missing-{uuid4().hex}",
                        },
                    )
            conn.rollback()
    finally:
        _live_cleanup_queue(created, [eid])


# --- B2 (live): end-to-end trigger wiring -------------------------------------


@needs_live_pg
def test_live_intake_name_conflict_persists_exactly_one_case() -> None:
    from app.agents.gabriel.intake import process_intake
    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    street = f"500 Identity Review Way {uuid4().hex}"
    url_a = f"https://example.com/listings/review-a-{uuid4().hex}"
    url_b = f"https://example.com/listings/review-b-{uuid4().hex}"
    created_props: list[str] = []
    created_reviews: list[str] = []
    run_ids: list[str] = []
    decision_ids: list[str] = []
    try:
        first = process_intake(_intake_body(name="Review Tower A", listing_url=url_a,
                                            street_address=street))
        assert first.outcome.value == "created"
        created_props.append(first.property_id)
        run_ids.append(first.run_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM properties WHERE street_address = %(s)s",
                            {"s": street})
                assert cur.fetchone()["n"] == 1
                cur.execute("SELECT COUNT(*) AS n FROM listing_evidence WHERE source_url = %(u)s",
                            {"u": url_a})
                assert cur.fetchone()["n"] == 1
        # Conflicting-name resubmission: review outcome, zero property writes.
        second = process_intake(_intake_body(name="Review Tower B", listing_url=url_b,
                                             street_address=street))
        assert second.outcome.value == "needs_identity_review"
        assert second.reason_code == "NAME_CONFLICT"
        run_ids.append(second.run_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM properties WHERE street_address = %(s)s",
                            {"s": street})
                assert cur.fetchone()["n"] == 1, "review must not create a property"
                cur.execute("SELECT COUNT(*) AS n FROM listing_evidence WHERE source_url = %(u)s",
                            {"u": url_b})
                assert cur.fetchone()["n"] == 0, "review must not attach evidence"
                cur.execute(
                    "SELECT event_id, details FROM agent_events "
                    "WHERE event_type = 'gabriel.intake.decision' AND run_id = %(run)s",
                    {"run": second.run_id},
                )
                persisted = cur.fetchone()
        assert persisted is not None
        decision_id = persisted["event_id"]
        decision_ids.append(decision_id)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM identity_reviews WHERE decision_event_id = %(eid)s",
                            {"eid": decision_id})
                row = cur.fetchone()
        assert row is not None, "review decision must persist its queue row atomically"
        created_reviews.append(row["review_id"])
        assert row["source"] == "intake"
        assert row["reason_code"] == "NAME_CONFLICT"
        assert row["status"] == "PENDING"
        assert row["occurrence_count"] == 1
        assert row["last_event_id"] == decision_id
        assert [str(pid) for pid in row["conflicting_property_ids"]] == [first.property_id]
        assert row["listing_url"] == url_b
        assert row["observed_identity"]["name"] == "Review Tower B"
        assert _live_occurrences(row["review_id"]) == [decision_id]
        # An identical repeat joins the same case instead of opening another.
        third = process_intake(_intake_body(name="Review Tower B", listing_url=url_b,
                                            street_address=street))
        assert third.outcome.value == "needs_identity_review"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_id FROM agent_events "
                    "WHERE event_type = 'gabriel.intake.decision' AND run_id = %(run)s",
                    {"run": third.run_id},
                )
                third_event = cur.fetchone()["event_id"]
        run_ids.append(third.run_id)
        decision_ids.append(third_event)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM identity_reviews WHERE dedup_key = %(k)s",
                            {"k": row["dedup_key"]})
                assert cur.fetchone()["n"] == 1
        joined = _live_review_row(row["review_id"])
        assert joined["occurrence_count"] == 2
        assert joined["last_event_id"] == third_event
        assert sorted(_live_occurrences(row["review_id"])) == sorted([decision_id, third_event])
    finally:
        _live_cleanup_queue(created_reviews, decision_ids)
        with get_connection() as conn:
            with conn.cursor() as cur:
                if run_ids:
                    # Queue rows are gone; remaining events for these runs are safe to remove.
                    cur.execute(
                        "DELETE FROM agent_events WHERE run_id = ANY(%(ids)s)",
                        {"ids": run_ids},
                    )
                if created_props:
                    cur.execute(
                        "DELETE FROM listing_evidence WHERE property_id = ANY(%(ids)s)",
                        {"ids": created_props},
                    )
                    cur.execute(
                        "DELETE FROM properties WHERE id = ANY(%(ids)s)",
                        {"ids": created_props},
                    )
            conn.commit()


def _discovery_run_context(run_id: str):  # type: ignore[no-untyped-def]
    import time

    from app.agents.gabriel.schemas import DiscoveryRunResult
    from app.agents.gabriel.service import _RunContext

    return _RunContext(
        run_id=run_id,
        started=time.perf_counter(),
        result=DiscoveryRunResult(
            source="osm_api",
            retrieved_at="2026-10-20T12:00:00+00:00",
            categories=["downtown_commercial"],
            limit=10,
            run_id=run_id,
        ),
    )


def test_discovery_review_skip_fixture_builds_valid_event() -> None:
    """Local guard: the live discovery fixture below is well-formed."""
    from app.agents.gabriel.service import _build_candidate_event

    event = _build_candidate_event(
        run_id=f"run-{uuid4().hex}", index=0, started=0.0, item=_discovery_item(),
        payload={"name": "Commerce Tower"}, dedup_key_value=None,
        outcome="skipped_duplicate", reason_code="AMBIGUOUS_ADDRESS",
        message="address matches multiple stored properties", mutation_status="NOT_ATTEMPTED",
        conflicting=_pids(2),
    )
    assert event.details["reason_code"] == "AMBIGUOUS_ADDRESS"
    assert len(event.details["conflicting_property_ids"]) == 2
    ctx = _discovery_run_context(event.run_id)
    assert ctx.result is not None and ctx.review_holds == 0


@needs_live_pg
def test_live_discovery_review_skip_persists_queue_row() -> None:
    from app.agents.gabriel.service import _record_skip
    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    run_id = f"run-{uuid4().hex}"
    pids = _pids(2)
    created_reviews: list[str] = []
    decision_ids: list[str] = []
    try:
        ctx = _discovery_run_context(run_id)
        _record_skip(
            ctx, _discovery_item(), 0, ctx.started, unusable=False,
            reason_code="AMBIGUOUS_ADDRESS",
            message="address matches multiple stored properties; identity ambiguous",
            payload={"name": "Commerce Tower"},
            conflicting=pids,
        )
        assert ctx.review_holds == 1
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_id FROM agent_events "
                    "WHERE event_type = 'gabriel.discovery.decision' AND run_id = %(run)s",
                    {"run": run_id},
                )
                found = cur.fetchone()
        assert found is not None
        decision_ids.append(found["event_id"])
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM identity_reviews WHERE decision_event_id = %(eid)s",
                            {"eid": found["event_id"]})
                row = cur.fetchone()
        assert row is not None, "discovery review must persist its queue row atomically"
        created_reviews.append(row["review_id"])
        assert row["source"] == "discovery"
        assert row["reason_code"] == "AMBIGUOUS_ADDRESS"
        assert row["status"] == "PENDING"
        assert row["occurrence_count"] == 1
        assert sorted(str(pid) for pid in row["conflicting_property_ids"]) == sorted(pids)
        assert row["provider"] == "osm_api"
        assert row["external_id"] == "way/1001"
        assert _live_occurrences(row["review_id"]) == [found["event_id"]]
    finally:
        _live_cleanup_queue(created_reviews, decision_ids)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_events WHERE run_id = %(run)s", {"run": run_id})
            conn.commit()


# --- B4 (live): occurrence integrity ------------------------------------------


def _live_synthetic_review_events(count: int):  # type: ignore[no-untyped-def]
    """Distinct intake review events sharing one exact identity question."""
    pids = _pids(2)
    return [_intake_event("URL_CONFLICT", pids) for _ in range(count)]


@needs_live_pg
def test_live_repeat_encounters_share_one_case_with_full_history() -> None:
    from app import identity_reviews_db as reviews
    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    events = _live_synthetic_review_events(2)
    created_reviews: list[str] = []
    event_ids = [e.event_id for e in events]
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for eid in event_ids:
                    _live_insert_parent_event(cur, eid)
            conn.commit()
        seen: list[str] = []
        for event in events:
            with get_connection() as conn:
                review_id = reviews.record_review_occurrence(conn, event)
                assert review_id is not None
                seen.append(review_id)
                conn.commit()
        assert seen[0] == seen[1], "identical repeats must join one pending case"
        created_reviews.append(seen[0])
        row = _live_review_row(seen[0])
        assert row["occurrence_count"] == 2
        assert row["last_event_id"] == event_ids[1]
        assert row["decision_event_id"] == event_ids[0]
        assert sorted(_live_occurrences(seen[0])) == sorted(event_ids)
    finally:
        _live_cleanup_queue(created_reviews, event_ids)


@needs_live_pg
def test_live_duplicate_event_is_rejected_without_double_count() -> None:
    from app import identity_reviews_db as reviews
    from app.db import DatabaseUnavailableError
    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    event = _live_synthetic_review_events(1)[0]
    created_reviews: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _live_insert_parent_event(cur, event.event_id)
            conn.commit()
        with get_connection() as conn:
            first = reviews.record_review_occurrence(conn, event)
            assert first is not None
            conn.commit()
            created_reviews.append(first)
        with get_connection() as conn:
            with pytest.raises(DatabaseUnavailableError):
                reviews.record_review_occurrence(conn, event)
            conn.rollback()
        row = _live_review_row(first)
        assert row["occurrence_count"] == 1
        assert _live_occurrences(first) == [event.event_id]
    finally:
        _live_cleanup_queue(created_reviews, [event.event_id])


@needs_live_pg
def test_live_concurrent_encounters_keep_every_occurrence() -> None:
    from app import identity_reviews_db as reviews
    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    events = _live_synthetic_review_events(2)
    created_reviews: list[str] = []
    event_ids = [e.event_id for e in events]
    errors: list[BaseException] = []
    seen: list[str] = []
    barrier = threading.Barrier(2)

    def _worker(event) -> None:  # type: ignore[no-untyped-def]
        try:
            barrier.wait(timeout=30)
            with get_connection() as conn:
                review_id = reviews.record_review_occurrence(conn, event)
                assert review_id is not None
                seen.append(review_id)
                conn.commit()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                for eid in event_ids:
                    _live_insert_parent_event(cur, eid)
            conn.commit()
        workers = [threading.Thread(target=_worker, args=(event,)) for event in events]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        assert not errors, f"concurrent workers failed: {errors!r}"
        assert len(seen) == 2
        assert seen[0] == seen[1], "concurrent identical encounters must join one case"
        created_reviews.append(seen[0])
        row = _live_review_row(seen[0])
        assert row["occurrence_count"] == 2
        assert sorted(_live_occurrences(seen[0])) == sorted(event_ids)
    finally:
        _live_cleanup_queue(created_reviews, event_ids)


# --- B5 (live): transaction atomicity ------------------------------------------


@needs_live_pg
def test_live_missing_event_rolls_back_review_insert() -> None:
    from app import identity_reviews_db as reviews
    from app.db import DatabaseUnavailableError
    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    event = _live_synthetic_review_events(1)[0]
    ctx = reviews.review_context_from_event(event)
    assert ctx is not None
    try:
        with get_connection() as conn:
            with pytest.raises(DatabaseUnavailableError):
                reviews.record_review_occurrence(conn, event)
            conn.rollback()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM identity_reviews WHERE dedup_key = %(k)s",
                            {"k": ctx["dedup_key"]})
                assert cur.fetchone()["n"] == 0
    finally:
        _live_cleanup_queue([], [])


@needs_live_pg
def test_live_queue_driver_failure_yields_503_without_durable_decision(monkeypatch) -> None:
    from fastapi import HTTPException, Response

    from app import identity_reviews_db as reviews
    from app.agents.gabriel.intake import process_intake
    from app.properties_db import get_connection
    from app.routes.discovery import submit_listing

    _live_config()
    _require_review_tables()
    # Fail beneath the queue writer (not around it): the first eligible
    # review context references a nonexistent event, so the real helper
    # hits a genuine foreign-key violation on live PostgreSQL.
    real_context = reviews.review_context_from_event
    poison = {"armed": True}

    def _poisoned(event):  # type: ignore[no-untyped-def]
        ctx = real_context(event)
        if ctx is not None and poison["armed"]:
            poison["armed"] = False
            ctx = dict(ctx)
            ctx["decision_event_id"] = f"evt-missing-{uuid4().hex}"
        return ctx

    monkeypatch.setattr(reviews, "review_context_from_event", _poisoned)
    street = f"600 Identity Review Way {uuid4().hex}"
    url_a = f"https://example.com/listings/atomic-a-{uuid4().hex}"
    url_b = f"https://example.com/listings/atomic-b-{uuid4().hex}"
    created_props: list[str] = []
    run_ids: list[str] = []
    try:
        first = process_intake(_intake_body(name="Atomic Tower A", listing_url=url_a,
                                            street_address=street))
        assert first.outcome.value == "created"
        created_props.append(first.property_id)
        run_ids.append(first.run_id)
        assert poison["armed"], "seed request must not consume the one-shot fault"
        with pytest.raises(HTTPException) as exc_info:
            submit_listing(
                _intake_body(name="Atomic Tower B", listing_url=url_b,
                             street_address=street),
                Response(),
                None,
            )
        assert exc_info.value.status_code == 503
        with get_connection() as conn:
            with conn.cursor() as cur:
                # No successful review decision survived the queue failure ...
                cur.execute(
                    "SELECT COUNT(*) AS n FROM agent_events "
                    "WHERE event_type = 'gabriel.intake.decision' "
                    "AND details->>'outcome' = 'needs_identity_review' "
                    "AND details->'observed'->>'listing_url' = %(url)s",
                    {"url": url_b},
                )
                assert cur.fetchone()["n"] == 0
                # ... but the established best-effort failure record did.
                cur.execute(
                    "SELECT COUNT(*) AS n FROM agent_events "
                    "WHERE event_type = 'gabriel.intake.decision' "
                    "AND details->>'outcome' = 'attempt_failed' "
                    "AND details->'observed'->>'listing_url' = %(url)s",
                    {"url": url_b},
                )
                assert cur.fetchone()["n"] == 1
                cur.execute(
                    "SELECT COUNT(*) AS n FROM identity_reviews WHERE listing_url = %(url)s",
                    {"url": url_b},
                )
                assert cur.fetchone()["n"] == 0
                cur.execute(
                    "SELECT COUNT(*) AS n FROM identity_review_occurrences occ "
                    "JOIN identity_reviews rev USING (review_id) "
                    "WHERE rev.listing_url = %(url)s",
                    {"url": url_b},
                )
                assert cur.fetchone()["n"] == 0
                cur.execute("SELECT COUNT(*) AS n FROM properties WHERE street_address = %(s)s",
                            {"s": street})
                assert cur.fetchone()["n"] == 1
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if run_ids:
                    cur.execute("DELETE FROM agent_events WHERE run_id = ANY(%(ids)s)",
                                {"ids": run_ids})
                # The failed request never returned its run id; its failure
                # record carries this execution's unique observed URL.
                cur.execute(
                    "DELETE FROM agent_events "
                    "WHERE event_type = 'gabriel.intake.decision' "
                    "AND details->'observed'->>'listing_url' = %(url)s",
                    {"url": url_b},
                )
                if created_props:
                    cur.execute("DELETE FROM listing_evidence WHERE property_id = ANY(%(ids)s)",
                                {"ids": created_props})
                    cur.execute("DELETE FROM properties WHERE id = ANY(%(ids)s)",
                                {"ids": created_props})
            conn.commit()


@needs_live_pg
def test_live_discovery_queue_driver_failure_aborts_loudly(monkeypatch) -> None:
    from app import identity_reviews_db as reviews
    from app.agents.gabriel.service import _record_skip
    from app.db import DatabaseUnavailableError
    from app.properties_db import get_connection

    _live_config()
    _require_review_tables()
    real_context = reviews.review_context_from_event

    def _poisoned(event):  # type: ignore[no-untyped-def]
        ctx = real_context(event)
        if ctx is not None:
            ctx = dict(ctx)
            ctx["decision_event_id"] = f"evt-missing-{uuid4().hex}"
        return ctx

    monkeypatch.setattr(reviews, "review_context_from_event", _poisoned)
    run_id = f"run-{uuid4().hex}"
    try:
        ctx = _discovery_run_context(run_id)
        with pytest.raises(DatabaseUnavailableError):
            _record_skip(
                ctx, _discovery_item(), 0, ctx.started, unusable=False,
                reason_code="AMBIGUOUS_ADDRESS",
                message="address matches multiple stored properties; identity ambiguous",
                payload={"name": "Commerce Tower"},
                conflicting=_pids(2),
            )
        # run_discovery maps DatabaseUnavailableError to abort + 503
        # (existing behavior); nothing durable may precede that mapping.
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM agent_events "
                    "WHERE event_type = 'gabriel.discovery.decision' AND run_id = %(run)s",
                    {"run": run_id},
                )
                assert cur.fetchone()["n"] == 0
                cur.execute(
                    "SELECT COUNT(*) AS n FROM identity_reviews WHERE run_id = %(run)s",
                    {"run": run_id},
                )
                assert cur.fetchone()["n"] == 0
    finally:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_events WHERE run_id = %(run)s", {"run": run_id})
            conn.commit()
