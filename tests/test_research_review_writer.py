"""IR-3C C1a: research identity-review writer extension (tests-first).

Local tests build REAL verdicts through the production builders
(build_research_verdicts / build_dossier_verdicts) and exercise the
review-eligibility gate, the research context mapping, and exact-match
canonical deduplication without any database. Live tests
(``GROWUP_PG_INTEGRATION=1``) verify research rows end to end on
isolated synthetic rows.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

from app import identity_reviews_db as reviews
from app.agents.gabriel.dossier import DossierSet, PropertyDossier
from app.agents.gabriel.extractor import (
    ExtractedProperty,
    ResearchIntelligence,
    ResultIntelligence,
    group_by_property,
)
from app.agents.gabriel.provenance import build_dossier_verdicts, build_research_verdicts
from app.agents.gabriel.ranking import (
    PropertyRanking,
    QCStatus,
    RankedCandidate,
    ScreenedLead,
)
from app.identity_reviews_db import IdentityReviewError
from app.observability.events import RetentionClass

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


# --- Real-builder fixtures ----------------------------------------------------


def _disputed_intel() -> ResearchIntelligence:
    a = ResultIntelligence(
        assignment_id="a1",
        query="downtown Dallas office",
        result_url="https://example.com/a",
        domain="example.com",
        title="Tower A",
        properties=[
            ExtractedProperty(
                name="Commerce Tower A",
                street_address="500 Commerce St",
                city="Dallas",
                state="TX",
            )
        ],
    )
    b = ResultIntelligence(
        assignment_id="a1",
        query="downtown Dallas office",
        result_url="https://example.com/b",
        domain="example.com",
        title="Tower B",
        properties=[
            ExtractedProperty(
                name="Commerce Tower B",
                street_address="500 Commerce St",
                city="Dallas",
                state="TX",
            )
        ],
    )
    return ResearchIntelligence(results=[a, b])


def _real_disputed_verdict(run_id: str = "run-c1a-1"):
    """One REAL research verdict for an address group with a name dispute."""
    intel = _disputed_intel()
    groups = group_by_property(intel.results)
    assert len(groups) == 1, "fixture expects one shared address group"
    key = groups[0].key
    assert key.startswith("addr:"), f"unexpected grouping key in fixture: {key!r}"
    candidate = RankedCandidate(
        key=key,
        qc_status=QCStatus.RESEARCH_REQUIRED,
        identity_disputes={"name": ["Commerce Tower A", "Commerce Tower B"]},
    )
    ranking = PropertyRanking(candidates=[candidate])
    verdicts = build_research_verdicts(
        run_id=run_id, correlation_id="corr-c1a-1", ranking=ranking, intel=intel
    )
    assert len(verdicts) == 1
    return verdicts[0]


def _real_dossier_verdict(run_id: str = "run-c1a-1"):
    """One REAL dossier verdict for the same disputed address group."""
    intel = _disputed_intel()
    groups = group_by_property(intel.results)
    assert len(groups) == 1
    dossier = PropertyDossier(
        key=groups[0].key,
        qc_status=QCStatus.RESEARCH_REQUIRED,
        identity_disputes={"name": ["Commerce Tower A", "Commerce Tower B"]},
    )
    dset = DossierSet(dossiers=[dossier])
    verdicts = build_dossier_verdicts(
        run_id=run_id,
        correlation_id="corr-c1a-1",
        dossier_set=dset,
        items=intel.results,
    )
    assert len(verdicts) == 1
    return verdicts[0]


def _real_screened_verdict():
    item = ResultIntelligence(
        assignment_id="a1",
        query="downtown Dallas retail",
        result_url="",
        domain="example.com",
        title="strip mall",
        properties=[ExtractedProperty(name="strip mall")],
    )
    intel = ResearchIntelligence(results=[item])
    groups = {group.key: group for group in group_by_property(intel.results)}
    assert list(groups.get("ungrouped:0").members) == [0]
    lead = ScreenedLead(
        result_url="",
        title="strip mall",
        rule="not_identifiable_property",
        reason="no identifiable property",
        source_index=0,
    )
    ranking = PropertyRanking(candidates=[], screened_leads=[lead])
    verdicts = build_research_verdicts(
        run_id="run-c1a-screened", correlation_id="corr-c1a-s", ranking=ranking, intel=intel
    )
    assert len(verdicts) == 1
    return verdicts[0]


def _with_details(event, **updates):
    details = dict(event.details)
    details.update(updates)
    return event.model_copy(update={"details": details})


# --- Eligibility (local, no I/O) ------------------------------------------------


def test_real_research_verdict_with_disputes_is_eligible() -> None:
    event = _real_disputed_verdict()
    assert event.details.get("identity_disputes"), "fixture must carry real disputes"
    ctx = reviews.review_context_from_event(event)
    assert ctx is not None, "disputed research verdicts must be queue-eligible"


def test_real_dossier_verdict_with_disputes_is_eligible() -> None:
    event = _real_dossier_verdict()
    assert event.event_type == "gabriel.dossier.verdict"
    assert event.details.get("identity_disputes"), "fixture must carry real disputes"
    ctx = reviews.review_context_from_event(event)
    assert ctx is not None, "disputed dossier verdicts must be queue-eligible"


def test_clean_verdict_without_disputes_is_ineligible() -> None:
    event = _real_disputed_verdict()
    details = dict(event.details)
    details.pop("identity_disputes", None)
    clean = event.model_copy(update={"details": details})
    assert reviews.review_context_from_event(clean) is None


def test_empty_disputes_object_is_ineligible() -> None:
    event = _with_details(_real_disputed_verdict(), identity_disputes={})
    assert reviews.review_context_from_event(event) is None


def test_screened_verdict_is_ineligible() -> None:
    event = _real_screened_verdict()
    assert event.details.get("decision") == "SCREENED"
    assert not event.details.get("identity_disputes"), "screened verdicts carry no disputes"
    assert reviews.review_context_from_event(event) is None


def test_non_permanent_research_verdict_fails_closed() -> None:
    event = _real_disputed_verdict().model_copy(
        update={"retention_class": RetentionClass.ACTIVITY}
    )
    with pytest.raises(IdentityReviewError):
        reviews.review_context_from_event(event)


def test_research_verdict_claiming_mutation_fails_closed() -> None:
    event = _with_details(_real_disputed_verdict(), mutation_status="COMMITTED")
    with pytest.raises(IdentityReviewError):
        reviews.review_context_from_event(event)


def test_research_verdict_with_operational_property_fails_closed() -> None:
    event = _real_disputed_verdict().model_copy(update={"property_id": str(uuid4())})
    with pytest.raises(IdentityReviewError):
        reviews.review_context_from_event(event)


def test_missing_candidate_identity_fails_closed() -> None:
    event = _real_disputed_verdict()
    details = dict(event.details)
    details.pop("candidate_ref", None)
    broken = event.model_copy(update={"details": details})
    with pytest.raises(IdentityReviewError):
        reviews.review_context_from_event(broken)


def test_blank_group_key_fails_closed() -> None:
    event = _real_disputed_verdict()
    ref = dict(event.details["candidate_ref"])
    ref["group_key"] = "  "
    broken = _with_details(event, candidate_ref=ref)
    with pytest.raises(IdentityReviewError):
        reviews.review_context_from_event(broken)


def test_malformed_dispute_structure_fails_closed() -> None:
    event = _with_details(_real_disputed_verdict(), identity_disputes={"name": "Tower A"})
    with pytest.raises(IdentityReviewError):
        reviews.review_context_from_event(event)


def test_dispute_field_without_variants_fails_closed() -> None:
    event = _with_details(_real_disputed_verdict(), identity_disputes={"name": []})
    with pytest.raises(IdentityReviewError):
        reviews.review_context_from_event(event)


# --- Research context mapping (local) -------------------------------------------


def test_research_context_fields() -> None:
    event = _real_disputed_verdict(run_id="run-ctx-1")
    ctx = reviews.review_context_from_event(event)
    assert ctx is not None
    assert ctx["source"] == "research"
    assert ctx["reason_code"] == "IDENTITY_DISPUTE"
    assert ctx["candidate_key"] == event.details["candidate_ref"]["group_key"]
    assert ctx["identity_disputes"] == event.details["identity_disputes"]
    assert ctx["conflicting_property_ids"] == []
    assert ctx["provider"] is None
    assert ctx["external_id"] is None
    assert ctx["listing_url"] is None
    assert ctx["observed_identity"] == event.details["observed"]
    assert ctx["decision_event_id"] == event.event_id
    assert ctx["run_id"] == "run-ctx-1"


def test_missing_basis_defaults_to_unknown() -> None:
    event = _real_disputed_verdict()
    ref = dict(event.details["candidate_ref"])
    ref.pop("basis", None)
    variant = _with_details(event, candidate_ref=ref)
    ctx = reviews.review_context_from_event(variant)
    assert ctx is not None
    assert json.loads(ctx["dedup_key"])["q"]["basis"] == "unknown"


# --- Canonical deduplication (local) ----------------------------------------------


def _dedup_dims(ctx) -> dict:  # type: ignore[no-untyped-def]
    return json.loads(ctx["dedup_key"])["q"]


def test_research_dedup_key_dimensions() -> None:
    ctx = reviews.review_context_from_event(_real_disputed_verdict())
    assert ctx is not None
    key = json.loads(ctx["dedup_key"])
    assert key["source"] == "research"
    assert key["reason"] == "IDENTITY_DISPUTE"
    assert key["pids"] == []
    dims = key["q"]
    assert dims["candidate_key"].startswith("addr:")
    assert dims["basis"] == "exact_address"
    assert dims["disputed_fields"] == ["name"]
    assert "run_id" not in dims, "addressed cases join across runs"


def test_research_and_dossier_verdicts_share_one_case() -> None:
    research = reviews.review_context_from_event(_real_disputed_verdict())
    dossier = reviews.review_context_from_event(_real_dossier_verdict())
    assert research is not None and dossier is not None
    assert research["dedup_key"] == dossier["dedup_key"]


def test_disputed_field_change_forks_a_new_case() -> None:
    base = reviews.review_context_from_event(_real_disputed_verdict())
    assert base is not None
    event = _with_details(
        _real_disputed_verdict(),
        identity_disputes={
            "name": ["Commerce Tower A", "Commerce Tower B"],
            "unit": ["Suite 100", "Suite 200"],
        },
    )
    forked = reviews.review_context_from_event(event)
    assert forked is not None
    assert forked["dedup_key"] != base["dedup_key"]
    assert _dedup_dims(forked)["disputed_fields"] == ["name", "unit"]


def test_dispute_value_evolution_keeps_one_case() -> None:
    base = reviews.review_context_from_event(_real_disputed_verdict())
    assert base is not None
    event = _with_details(
        _real_disputed_verdict(),
        identity_disputes={"name": ["Commerce Tower A", "Commerce Tower C"]},
    )
    evolved = reviews.review_context_from_event(event)
    assert evolved is not None
    assert evolved["dedup_key"] == base["dedup_key"]


def test_ungrouped_candidates_are_scoped_to_their_run() -> None:
    first = _real_disputed_verdict(run_id="run-u-1")
    ref = dict(first.details["candidate_ref"])
    ref["group_key"] = "ungrouped:3"
    ref["basis"] = "no_identity"
    variant = _with_details(first, candidate_ref=ref)
    ctx = reviews.review_context_from_event(variant)
    assert ctx is not None
    dims = _dedup_dims(ctx)
    assert dims["candidate_key"] == "ungrouped:3"
    assert dims["run_id"] == "run-u-1"
    second = _real_disputed_verdict(run_id="run-u-2")
    ref2 = dict(second.details["candidate_ref"])
    ref2["group_key"] = "ungrouped:3"
    ref2["basis"] = "no_identity"
    other = reviews.review_context_from_event(_with_details(second, candidate_ref=ref2))
    assert other is not None
    assert other["dedup_key"] != ctx["dedup_key"], "same ungrouped index, other run"


def test_addressed_candidates_join_across_runs() -> None:
    first = reviews.review_context_from_event(_real_disputed_verdict(run_id="run-x-1"))
    second = reviews.review_context_from_event(_real_disputed_verdict(run_id="run-x-2"))
    assert first is not None and second is not None
    assert first["dedup_key"] == second["dedup_key"]


def test_canonical_serialization_is_deterministic() -> None:
    first = reviews.review_context_from_event(_real_disputed_verdict()) or {}
    second = reviews.review_context_from_event(_real_disputed_verdict()) or {}
    assert first["dedup_key"] == second["dedup_key"]
    raw = first["dedup_key"]
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))


# --- Writer parameters (local fakes, no I/O) ---------------------------------------


class _FakeCursor:
    def __init__(self, review_id: str):
        self.statements: list[tuple[str, dict]] = []
        self._review_id = review_id

    def execute(self, sql, params=None):  # type: ignore[no-untyped-def]
        self.statements.append((sql, dict(params or {})))

    def fetchone(self):  # type: ignore[no-untyped-def]
        return {"review_id": self._review_id}

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *exc):  # type: ignore[no-untyped-def]
        return False


class _FakeConn:
    def __init__(self, review_id: str):
        self.cursor_obj = _FakeCursor(review_id)

    def cursor(self):  # type: ignore[no-untyped-def]
        return self.cursor_obj


def test_writer_persists_research_columns() -> None:
    event = _real_disputed_verdict()
    conn = _FakeConn(review_id=str(uuid4()))
    review_id = reviews.record_review_occurrence(conn, event)
    assert review_id == conn.cursor_obj._review_id
    upsert_sql, params = conn.cursor_obj.statements[0]
    assert "candidate_key" in upsert_sql
    assert "identity_disputes" in upsert_sql
    assert params["candidate_key"] == event.details["candidate_ref"]["group_key"]
    assert json.loads(params["identity_disputes"]) == event.details["identity_disputes"]
    assert params["conflicting_property_ids"] == []
    assert params["source"] == "research"
    assert params["reason_code"] == "IDENTITY_DISPUTE"


def test_writer_passes_null_research_columns_for_operational_events() -> None:
    from app.agents.gabriel.intake import (
        ListingIntakeRequest,
        _DecisionContext,
        _decision_event,
    )

    body = ListingIntakeRequest(
        name="Commerce Tower B",
        street_address="500 Commerce Street",
        city="Dallas",
        state="TX",
        listing_url="https://example.com/listings/op",
        source_name="Harborline Brokerage Records",
        observed_at="2026-10-20T12:00:00+00:00",
    )
    ctx_in = _DecisionContext(
        body=body,
        conn=None,
        run_id="run-op-1",
        started=0.0,
        source_url=str(body.listing_url),
    )
    event = _decision_event(
        ctx_in,
        "needs_identity_review",
        "NAME_CONFLICT",
        "conflicting building name",
        "NOT_ATTEMPTED",
        (),
        (str(uuid4()),),
        None,
    )
    ctx = reviews.review_context_from_event(event)
    assert ctx is not None
    assert ctx["candidate_key"] is None
    assert ctx["identity_disputes"] is None
    conn = _FakeConn(review_id=str(uuid4()))
    assert reviews.record_review_occurrence(conn, event) is not None
    _, params = conn.cursor_obj.statements[0]
    assert params["candidate_key"] is None
    assert params["identity_disputes"] is None


# --- Live PostgreSQL (gated) --------------------------------------------------------


def _live_config() -> None:
    from app.properties_db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor():
                pass
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL unavailable for C1a live tests: {exc}")


def _live_require_review_tables() -> None:
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


def _live_insert_parent_event(cur, event_id: str, event_type: str) -> None:  # type: ignore[no-untyped-def]
    cur.execute(
        "INSERT INTO agent_events (event_id, occurred_at, agent, run_id, event_type, "
        "severity, summary, details, evidence_refs, retention_class) "
        "VALUES (%(eid)s, NOW(), 'gabriel', %(run)s, %(etype)s, "
        "'INFO', 'synthetic C1a parent', '{}', '[]', 'permanent')",
        {"eid": event_id, "run": f"run-{uuid4().hex}", "etype": event_type},
    )


def _live_cleanup_queue(review_ids: list[str], event_ids: list[str]) -> None:
    from app.properties_db import get_connection

    rids = [str(item) for item in review_ids if item]
    eids = [str(item) for item in event_ids if item]
    if not rids and not eids:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            if rids:
                cur.execute(
                    "DELETE FROM identity_review_occurrences WHERE review_id = ANY(%(ids)s)",
                    {"ids": rids},
                )
                cur.execute(
                    "DELETE FROM identity_reviews WHERE review_id = ANY(%(ids)s)",
                    {"ids": rids},
                )
            if eids:
                cur.execute(
                    "DELETE FROM agent_events WHERE event_id = ANY(%(ids)s)", {"ids": eids}
                )
        conn.commit()


@needs_live_pg
def test_live_research_review_end_to_end() -> None:
    from app.properties_db import get_connection

    _live_config()
    _live_require_review_tables()
    research = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    dossier = _real_dossier_verdict(run_id=f"run-{uuid4().hex}")
    event_ids = [research.event_id, dossier.event_id]
    created: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _live_insert_parent_event(cur, research.event_id, research.event_type)
                _live_insert_parent_event(cur, dossier.event_id, dossier.event_type)
            conn.commit()
        with get_connection() as conn:
            first = reviews.record_review_occurrence(conn, research)
            assert first is not None
            conn.commit()
            created.append(first)
        with get_connection() as conn:
            second = reviews.record_review_occurrence(conn, dossier)
            assert second == first, "research and dossier verdicts must join one case"
            conn.commit()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM identity_reviews WHERE review_id = %(rid)s",
                    {"rid": first},
                )
                row = cur.fetchone()
                cur.execute(
                    "SELECT event_id FROM identity_review_occurrences "
                    "WHERE review_id = %(rid)s",
                    {"rid": first},
                )
                occurrences = sorted(r["event_id"] for r in cur.fetchall())
        assert row["source"] == "research"
        assert row["reason_code"] == "IDENTITY_DISPUTE"
        assert row["candidate_key"] == research.details["candidate_ref"]["group_key"]
        assert row["identity_disputes"] == research.details["identity_disputes"]
        assert row["conflicting_property_ids"] == []
        assert row["occurrence_count"] == 2
        assert occurrences == sorted(event_ids)
    finally:
        _live_cleanup_queue(created, event_ids)


@needs_live_pg
def test_live_ungrouped_research_cases_stay_run_scoped() -> None:
    from app.properties_db import get_connection

    _live_config()
    _live_require_review_tables()
    first = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    second = _real_disputed_verdict(run_id=f"run-{uuid4().hex}")
    ref = dict(first.details["candidate_ref"])
    ref["group_key"] = f"ungrouped:{uuid4().hex}"
    ref["basis"] = "no_identity"
    first = _with_details(first, candidate_ref=ref)
    ref2 = dict(second.details["candidate_ref"])
    ref2["group_key"] = ref["group_key"]
    ref2["basis"] = "no_identity"
    second = _with_details(second, candidate_ref=ref2)
    event_ids = [first.event_id, second.event_id]
    created: list[str] = []
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _live_insert_parent_event(cur, first.event_id, first.event_type)
                _live_insert_parent_event(cur, second.event_id, second.event_type)
            conn.commit()
        with get_connection() as conn:
            rid_a = reviews.record_review_occurrence(conn, first)
            assert rid_a is not None
            conn.commit()
            created.append(rid_a)
        with get_connection() as conn:
            rid_b = reviews.record_review_occurrence(conn, second)
            assert rid_b is not None
            conn.commit()
            created.append(rid_b)
        assert rid_a != rid_b, "same ungrouped key on different runs must fork"
    finally:
        _live_cleanup_queue(created, event_ids)
