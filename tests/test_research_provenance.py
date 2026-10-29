"""Durable research/dossier verdict tests (G6-1).

Final candidate dispositions persist as permanent agent_events through
atomic SQLite batch acceptance plus idempotent PostgreSQL delivery.
No provider calls, no operational writes. Live PG tests gate on
GROWUP_PG_INTEGRATION=1 with cleanup by run_id.
"""

from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import urlparse
from uuid import uuid4

import pytest

from app.db import DatabaseUnavailableError
from app.observability.events import AgentEvent, AgentId, RetentionClass
from app.observability.outbox import Outbox

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0


@pytest.fixture(autouse=True)
def _obs_enabled(monkeypatch):
    monkeypatch.setenv("OBS_ENABLED", "1")


def _event(event_id=None, **overrides):
    from app.observability.events import Severity

    params = {
        "agent": AgentId.GABRIEL,
        "run_id": "run verdict batch 1",
        "event_type": "gabriel.research.candidate_decision",
        "summary": "research decision: PASS (QC_PASS)",
        "severity": Severity.INFO,
        "retention_class": RetentionClass.PERMANENT,
        "details": {"decision": "PASS"},
    }
    if event_id is not None:
        params["event_id"] = event_id
    params.update(overrides)
    return AgentEvent(**params)


def _box(tmp_path, name="verdict-outbox.sqlite3"):
    box = Outbox(tmp_path / name)
    box.initialize()
    return box


class _FakeCursor:
    def __init__(self, pg):
        self._pg = pg
        self._fetchone: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        pg = self._pg
        pg.executes.append(query)
        if "INSERT INTO agent_events" in query:
            pg.inserts += 1
            if pg.fail_after is not None and pg.inserts > pg.fail_after:
                raise DatabaseUnavailableError("injected insert failure")
            event_id = params["event_id"]
            if event_id in pg.rows or event_id in pg.staged:
                self._fetchone = None  # ON CONFLICT DO NOTHING conflict path
                pg.conflict_id = event_id
                return
            pg.staged[event_id] = {
                "event_id": event_id,
                "occurred_at": params["occurred_at"],
                "agent": params["agent"],
                "run_id": params["run_id"],
                "correlation_id": params.get("correlation_id") or "",
                "property_id": params.get("property_id"),
                "event_type": params["event_type"],
                "severity": params["severity"],
                "summary": params["summary"],
                "details": json.loads(params["details"]),
                "evidence_refs": json.loads(params["evidence_refs"]),
                "parent_event_id": params.get("parent_event_id"),
                "retention_class": params["retention_class"],
            }
            self._fetchone = {"event_id": event_id} if "RETURNING" in query else None
        elif query.strip().upper().startswith("SELECT"):
            row = pg.rows.get(pg.conflict_id) or pg.staged.get(pg.conflict_id)
            self._fetchone = row

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, pg):
        self._pg = pg

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _FakeCursor(self._pg)

    def commit(self):
        self._pg.commits += 1
        self._pg.rows.update(self._pg.staged)
        self._pg.staged = {}

    def rollback(self):
        self._pg.rollbacks += 1
        self._pg.staged = {}


class _FakePG:
    """Scripted PostgreSQL stand-in: staged rows flush only on commit."""

    def __init__(self, down=False, fail_after=None):
        self.down = down
        self.fail_after = fail_after
        self.rows: dict[str, dict] = {}
        self.staged: dict[str, dict] = {}
        self.commits = 0
        self.rollbacks = 0
        self.inserts = 0
        self.executes: list[str] = []
        self.conflict_id: str | None = None

    def __call__(self, config=None):
        if self.down:
            raise DatabaseUnavailableError("pg unavailable")
        return _FakeConn(self)


def _patch_pg(monkeypatch, pg):
    import app.observability.store as store

    monkeypatch.setattr(store, "get_connection", pg)


# --- Outbox.accept_all ---


def test_accept_all_success(tmp_path) -> None:
    box = _box(tmp_path)
    events = [_event(f"batch-ok-{i}") for i in range(3)]
    ids = box.accept_all(events)
    assert ids == [e.event_id for e in events]
    assert box.count_pending() == 3


def test_accept_all_empty_noop(tmp_path) -> None:
    from app.observability.outbox import Outbox as OutboxCls

    target = tmp_path / "never-created.sqlite3"
    box = OutboxCls(target)
    assert box.accept_all([]) == []
    assert not target.exists()


def test_accept_all_rollback_on_midbatch_conflict(tmp_path) -> None:
    box = _box(tmp_path)
    box.accept(_event("conflict-id", details={"decision": "PASS"}))
    batch = [
        _event("batch-new-1"),
        _event("conflict-id", details={"decision": "EXCLUDED"}),
        _event("batch-new-2"),
    ]
    from app.observability.outbox import OutboxConflictError

    with pytest.raises(OutboxConflictError):
        box.accept_all(batch)
    assert box.count_pending() == 1  # only the pre-existing row
    assert {p.event.event_id for p in box.pending(limit=10)} == {"conflict-id"}


def test_accept_all_identical_resubmission(tmp_path) -> None:
    box = _box(tmp_path)
    events = [_event(f"resub-000{i}") for i in range(2)]
    assert box.accept_all(events) == [e.event_id for e in events]
    assert box.accept_all(events) == [e.event_id for e in events]
    assert box.count_pending() == 2


def test_accept_all_rejects_nonpermanent(tmp_path) -> None:
    box = _box(tmp_path)
    good = _event("good-0001")
    bad = _event("bad-0001", retention_class=RetentionClass.ACTIVITY)
    with pytest.raises(ValueError):
        box.accept_all([good, bad])
    assert box.count_pending() == 0


def test_accept_all_duplicate_ids_in_batch_fail_closed(tmp_path) -> None:
    box = _box(tmp_path)
    from app.observability.outbox import OutboxConflictError

    with pytest.raises(OutboxConflictError):
        box.accept_all([
            _event("dup-id-01", details={"decision": "PASS"}),
            _event("dup-id-01", details={"decision": "EXCLUDED"}),
        ])
    assert box.count_pending() == 0


def test_accept_all_duplicate_identical_ids_ok(tmp_path) -> None:
    box = _box(tmp_path)
    first = _event("same-id-1")
    assert box.accept_all([first, first]) == ["same-id-1", "same-id-1"]
    assert box.count_pending() == 1


# --- require_durable_batch ---


def test_batch_all_pg_confirmed(tmp_path, monkeypatch) -> None:
    from app.observability.emitter import require_durable_batch

    box = _box(tmp_path)
    pg = _FakePG()
    _patch_pg(monkeypatch, pg)
    outcome = require_durable_batch([_event(f"ok-0000{i}") for i in range(3)], outbox=box)
    assert outcome.pending is False
    assert outcome.persisted_pg_count == 3
    assert box.count_pending() == 0
    assert len(pg.rows) == 3


def test_batch_pg_down_stays_pending(tmp_path, monkeypatch) -> None:
    from app.observability.emitter import require_durable_batch

    box = _box(tmp_path)
    _patch_pg(monkeypatch, _FakePG(down=True))
    events = [_event(f"pend-0000{i}") for i in range(2)]
    outcome = require_durable_batch(events, outbox=box)
    assert outcome.pending is True
    assert outcome.persisted_pg_count == 0
    assert box.count_pending() == 2


def test_batch_outbox_down_pg_fallback_atomic(tmp_path, monkeypatch) -> None:
    from app.observability.emitter import require_durable_batch
    from app.observability.outbox import OutboxUnavailableError

    box = _box(tmp_path)
    monkeypatch.setattr(box, "accept_all", lambda events: (_ for _ in ()).throw(
        OutboxUnavailableError("sqlite unavailable")))
    pg = _FakePG()
    _patch_pg(monkeypatch, pg)
    events = [_event(f"fb-0000{i}") for i in range(3)]
    outcome = require_durable_batch(events, outbox=box)
    assert outcome.pending is False
    assert outcome.persisted_pg_count == 3
    assert pg.commits == 1  # one transaction, not per-event commits
    assert len(pg.rows) == 3


def test_batch_both_down_raises(tmp_path, monkeypatch) -> None:
    from app.observability.emitter import DurabilityError, require_durable_batch
    from app.observability.outbox import OutboxUnavailableError

    box = _box(tmp_path)
    monkeypatch.setattr(box, "accept_all", lambda events: (_ for _ in ()).throw(
        OutboxUnavailableError("sqlite unavailable")))
    pg = _FakePG(down=True)
    _patch_pg(monkeypatch, pg)
    with pytest.raises(DurabilityError):
        require_durable_batch([_event("lost-0001")], outbox=box)
    assert pg.inserts == 0


def test_batch_conflict_no_pg_fallback(tmp_path, monkeypatch) -> None:
    from app.observability.emitter import DurabilityError, require_durable_batch

    box = _box(tmp_path)
    box.accept(_event("taken-01", details={"decision": "PASS"}))
    pg = _FakePG()
    _patch_pg(monkeypatch, pg)
    with pytest.raises(DurabilityError):
        require_durable_batch([_event("taken-01", details={"decision": "EXCLUDED"})], outbox=box)
    assert pg.inserts == 0  # fail closed without touching PostgreSQL


def test_batch_empty_noop(tmp_path, monkeypatch) -> None:
    from app.observability.emitter import require_durable_batch

    pg = _FakePG()
    _patch_pg(monkeypatch, pg)
    outcome = require_durable_batch([], outbox=None)
    assert outcome.pending is False
    assert outcome.persisted_pg_count == 0
    assert pg.executes == []


def test_batch_pg_fallback_rollback(tmp_path, monkeypatch) -> None:
    from app.observability.emitter import DurabilityError, require_durable_batch
    from app.observability.outbox import OutboxUnavailableError

    box = _box(tmp_path)
    monkeypatch.setattr(box, "accept_all", lambda events: (_ for _ in ()).throw(
        OutboxUnavailableError("sqlite unavailable")))
    pg = _FakePG(fail_after=1)  # second INSERT fails
    _patch_pg(monkeypatch, pg)
    with pytest.raises(DurabilityError):
        require_durable_batch([_event(f"rb-0000{i}") for i in range(3)], outbox=box)
    assert pg.rollbacks >= 1
    assert pg.rows == {}  # complete rollback, nothing persisted


# --- Verdict builders ---


def _brave_result(title, snippet, url="https://example.com/story", published_at=None):
    from app.agents.gabriel.sources.brave import BraveSearchResult

    global _RANK
    _RANK += 1
    return BraveSearchResult(
        assignment_id="gsp-prov001", query="test query", title=title, url=url,
        snippet=snippet, domain=urlparse(url).netloc, provider="brave",
        retrieved_at=_RETRIEVED, published_at=published_at, rank=_RANK,
    )


def _intel(*results):
    from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence

    return ResearchIntelligence(results=[extract_intelligence(item) for item in results])


def _listing(price="$40 million", url="https://example.com/meridian", published_at="2026-10-20"):
    return _brave_result(
        "Meridian Tower listed for sale",
        f"Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale "
        f"with an asking price of {price}. Dallas County.",
        url=url, published_at=published_at,
    )


def _research_verdicts(ranking, intel, run_id="run-1"):
    from app.agents.gabriel.provenance import build_research_verdicts

    return build_research_verdicts(
        run_id=run_id, correlation_id="corr-1", ranking=ranking, intel=intel)


def _assert_verdict_shape(event, run_id="run-1"):
    assert event.agent == AgentId.GABRIEL
    assert event.run_id == run_id
    assert event.correlation_id == "corr-1"
    assert event.property_id is None
    assert event.retention_class == RetentionClass.PERMANENT
    details = event.details
    for key in ("decision", "reason_codes", "message", "qc_status", "insufficient_data",
                "candidate_ref", "observed", "inferred", "result", "unresolved",
                "mutation_status"):
        assert key in details, key
    assert details["mutation_status"] == "NOT_ATTEMPTED"
    ref = details["candidate_ref"]
    for key in ("group_key", "basis", "ordinal"):
        assert key in ref, key


def test_research_verdict_pass() -> None:
    from app.agents.gabriel.ranking import rank_intelligence

    ranking = rank_intelligence(_intel(_listing()))
    (verdict,) = _research_verdicts(ranking, _intel(_listing()))
    _assert_verdict_shape(verdict)
    assert verdict.event_type == "gabriel.research.candidate_decision"
    assert verdict.details["decision"] == "PASS"
    assert verdict.details["insufficient_data"] is False
    assert verdict.details["candidate_ref"]["group_key"].startswith("addr:")
    assert "PASS" in verdict.summary


def test_research_verdict_research_required() -> None:
    from app.agents.gabriel.ranking import rank_intelligence

    intel = _intel(_brave_result(
        "Tower mentioned",
        "Meridian Tower at 500 Commerce St, Dallas, TX came up in conversation.",
        url="https://example.com/mention",
    ))
    ranking = rank_intelligence(intel)
    (verdict,) = _research_verdicts(ranking, intel)
    assert verdict.details["decision"] == "RESEARCH_REQUIRED"
    assert verdict.details["insufficient_data"] is True
    assert "INSUFFICIENT_DATA" in verdict.details["reason_codes"]


def test_research_verdict_excluded_preserves_rule() -> None:
    from app.agents.gabriel.ranking import rank_intelligence

    intel = _intel(_brave_result(
        "Tower demolished",
        "Meridian Tower at 500 Commerce St, Dallas, TX was demolished last year.",
        url="https://example.com/demolished",
    ))
    ranking = rank_intelligence(intel)
    (candidate,) = ranking.candidates
    assert candidate.exclusions
    (verdict,) = _research_verdicts(ranking, intel)
    assert verdict.details["decision"] == "EXCLUDED"
    assert candidate.exclusions[0].rule in verdict.details["reason_codes"]
    refs = [r.ref for r in verdict.evidence_refs]
    assert candidate.exclusions[0].source_url in refs
    (record,) = verdict.details["exclusions"]
    assert record["rule"] == candidate.exclusions[0].rule
    assert record["evidence"] == candidate.exclusions[0].evidence[:300]
    assert record["date"] == candidate.exclusions[0].date


def test_research_verdict_deferred() -> None:
    from app.agents.gabriel.ranking import rank_intelligence

    intel = _intel(_brave_result(
        "Houston tower listed",
        "Bayou Tower at 500 Smith St, Houston, TX is listed for sale with an "
        "asking price of $50 million. The Harris County office tower drew bids.",
        url="https://example.com/bayou",
        published_at="2026-10-20",
    ))
    ranking = rank_intelligence(intel)
    (verdict,) = _research_verdicts(ranking, intel)
    assert verdict.details["decision"] == "DEFERRED"
    assert "outside_operating_area" in verdict.details["reason_codes"]


def test_research_verdict_identity_dispute() -> None:
    from app.agents.gabriel.ranking import rank_intelligence

    intel = _intel(
        _brave_result("Alpha Tower", "Alpha Tower at 100 Main St, Dallas, TX is listed.",
                      url="https://example.com/a"),
        _brave_result("Beta Tower", "Beta Tower at 100 Main St, Dallas, TX is listed.",
                      url="https://example.com/b"),
    )
    ranking = rank_intelligence(intel)
    (verdict,) = _research_verdicts(ranking, intel)
    assert verdict.details["identity_disputes"]["name"] == ["Alpha Tower", "Beta Tower"]
    assert "IDENTITY_DISPUTE" in verdict.details["reason_codes"]
    assert verdict.details["observed"]["name"] == "Alpha Tower"  # verbatim, flagged disputed


def test_research_verdict_addressless() -> None:
    from app.agents.gabriel.ranking import rank_intelligence

    intel = _intel(
        _brave_result("Tower profile", "Meridian Tower in Dallas drew investors.",
                      url="https://example.com/profile"),
        _brave_result("Tower profile", "Meridian Tower in Dallas drew investors.",
                      url="https://example.com/profile"),
    )
    ranking = rank_intelligence(intel)
    assert len(ranking.candidates) == 1
    (verdict,) = _research_verdicts(ranking, intel)
    ref = verdict.details["candidate_ref"]
    assert ref["group_key"].startswith("doc:")
    assert verdict.details["observed"]["street_address"] is None
    assert verdict.details["inferred"]["street_address"] is None


def test_research_verdicts_multi_candidate_run() -> None:
    from app.agents.gabriel.ranking import rank_intelligence

    intel = _intel(
        _listing("$40 million", url="https://example.com/meridian"),
        _brave_result(
            "Bayou Tower listed",
            "Bayou Tower at 500 Smith St, Houston, TX is listed for sale. Harris County.",
            url="https://example.com/bayou",
            published_at="2026-10-20",
        ),
    )
    ranking = rank_intelligence(intel)
    assert len(ranking.candidates) == 2
    verdicts = _research_verdicts(ranking, intel, run_id="run-multi")
    assert len(verdicts) == 2  # exactly one verdict per final candidate
    assert {v.details["candidate_ref"]["ordinal"] for v in verdicts} == {0, 1}
    assert {v.details["candidate_ref"]["group_key"] for v in verdicts} == {
        c.key for c in ranking.candidates}
    assert all(v.run_id == "run-multi" for v in verdicts)
    assert len({v.event_id for v in verdicts}) == 2


def test_dossier_verdict_ready() -> None:
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.provenance import build_dossier_verdicts

    intel = _intel(_listing())
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    (dossier,) = dossier_set.dossiers
    (verdict,) = build_dossier_verdicts(
        run_id="run-d", correlation_id="corr-1",
        dossier_set=dossier_set, items=intel.results)
    _assert_verdict_shape(verdict, run_id="run-d")
    assert verdict.event_type == "gabriel.dossier.verdict"
    assert verdict.details["decision"] == "READY"
    assert verdict.details["result"]["handoff_agents"]
    assert verdict.details["candidate_ref"]["group_key"] == dossier.key


def test_dossier_verdict_excluded_preserves_rule() -> None:
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.provenance import build_dossier_verdicts

    intel = _intel(_brave_result(
        "Old mill demolished",
        "The Old Mill at 9 River Rd, Dallas, TX was demolished last year. "
        "The Dallas County site is cleared.",
        url="https://example.com/mill",
    ))
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    (dossier,) = dossier_set.dossiers
    assert dossier.exclusions
    (verdict,) = build_dossier_verdicts(
        run_id="run-d", correlation_id="corr-1",
        dossier_set=dossier_set, items=intel.results)
    assert verdict.details["decision"] == "EXCLUDED"
    assert dossier.exclusions[0].rule in verdict.details["reason_codes"]
    assert dossier.exclusions[0].source_url in [r.ref for r in verdict.evidence_refs]
    (record,) = verdict.details["exclusions"]
    assert record["rule"] == dossier.exclusions[0].rule
    assert record["evidence"] == dossier.exclusions[0].evidence[:300]


# --- Emission integration ---


def _verdict_rows(pg, event_type):
    return [row for row in pg.rows.values() if row["event_type"] == event_type]


def test_research_run_persists_verdicts(monkeypatch, tmp_path) -> None:
    import app.agents.gabriel.research as research
    from app.agents.gabriel.planner import plan_initial_searches

    monkeypatch.setenv("OBS_OUTBOX_PATH", str(tmp_path / "outbox.sqlite3"))
    from app.config import DatabaseConfig
    monkeypatch.setattr(research, "get_database_config",
                        lambda: DatabaseConfig(host="h", port=5432, name="d", user="u", password="p"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")
    state = {"used": 0}

    def _reserve(count):
        state["used"] += count
        return {"month": "2026-10", "used": state["used"], "ceiling": 100, "remaining": 99}

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    monkeypatch.setattr(research, "read_budget",
                        lambda: {"month": "2026-10", "used": 0, "ceiling": 100, "remaining": 100})
    calls: list = []

    def _execute(query, assignment_id, timeout):
        calls.append(assignment_id)
        if len(calls) == 1:
            return [_listing()]
        if len(calls) == 2:
            return [_brave_result(
                "Bayou Tower listed",
                "Bayou Tower at 500 Smith St, Dallas, TX is listed for sale. Dallas County.",
                url="https://example.com/bayou", published_at="2026-10-20")]
        return []

    monkeypatch.setattr(research, "execute_query", _execute)
    pg = _FakePG()
    _patch_pg(monkeypatch, pg)
    assignments = [a.model_dump() for a in plan_initial_searches()]
    result = research.run_research(research.ResearchRunRequest.model_validate(
        {"assignments": assignments, "include_ranking": True}))
    assert result.provenance_pending is False
    rows = _verdict_rows(pg, "gabriel.research.candidate_decision")
    assert len(rows) == 2
    assert {row["run_id"] for row in rows} == {result.run_id}
    assert all(row["property_id"] is None for row in rows)


def test_extraction_only_run_emits_no_verdicts(monkeypatch, tmp_path) -> None:
    import app.agents.gabriel.research as research
    from app.agents.gabriel.planner import plan_initial_searches

    monkeypatch.setenv("OBS_OUTBOX_PATH", str(tmp_path / "outbox.sqlite3"))
    from app.config import DatabaseConfig
    monkeypatch.setattr(research, "get_database_config",
                        lambda: DatabaseConfig(host="h", port=5432, name="d", user="u", password="p"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")
    monkeypatch.setattr(research, "reserve_requests",
                        lambda c: {"month": "m", "used": 1, "ceiling": 100, "remaining": 99})
    monkeypatch.setattr(research, "read_budget",
                        lambda: {"month": "m", "used": 0, "ceiling": 100, "remaining": 100})
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [_listing()])
    pg = _FakePG()
    _patch_pg(monkeypatch, pg)
    assignments = [a.model_dump() for a in plan_initial_searches()]
    result = research.run_research(research.ResearchRunRequest.model_validate(
        {"assignments": assignments, "include_ranking": False}))
    assert result.provenance_pending is False
    assert _verdict_rows(pg, "gabriel.research.candidate_decision") == []


def test_pending_then_recovery_delivers(monkeypatch, tmp_path) -> None:
    import app.agents.gabriel.research as research
    from app.agents.gabriel.planner import plan_initial_searches
    from app.observability.outbox import Outbox
    from app.observability.recovery import recover_once

    outbox_path = tmp_path / "outbox.sqlite3"
    monkeypatch.setenv("OBS_OUTBOX_PATH", str(outbox_path))
    from app.config import DatabaseConfig
    monkeypatch.setattr(research, "get_database_config",
                        lambda: DatabaseConfig(host="h", port=5432, name="d", user="u", password="p"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")
    monkeypatch.setattr(research, "reserve_requests",
                        lambda c: {"month": "m", "used": 1, "ceiling": 100, "remaining": 99})
    monkeypatch.setattr(research, "read_budget",
                        lambda: {"month": "m", "used": 0, "ceiling": 100, "remaining": 100})
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [_listing()])
    pg = _FakePG(down=True)
    _patch_pg(monkeypatch, pg)
    assignments = [a.model_dump() for a in plan_initial_searches()]
    result = research.run_research(research.ResearchRunRequest.model_validate(
        {"assignments": assignments, "include_ranking": True}))
    assert result.provenance_pending is True  # durable in SQLite, PG pending
    assert _verdict_rows(pg, "gabriel.research.candidate_decision") == []
    box = Outbox(outbox_path)
    assert box.count_pending() >= 1
    pg.down = False  # PostgreSQL recovers; the existing worker drains the outbox
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    report = recover_once(box, purge_delivered_days=None, now=future)
    assert report.delivered >= 1
    rows = _verdict_rows(pg, "gabriel.research.candidate_decision")
    assert len(rows) == 1
    assert rows[0]["run_id"] == result.run_id
    assert box.count_pending() == 0


def test_both_stores_down_research_raises(monkeypatch, tmp_path) -> None:
    import app.agents.gabriel.research as research
    from app.agents.gabriel.planner import plan_initial_searches
    from app.observability.emitter import DurabilityError

    monkeypatch.setenv("OBS_OUTBOX_PATH", str(tmp_path / "missing-dir" / "outbox.sqlite3"))
    import app.agents.gabriel.provenance as provenance

    def _broken(*args, **kwargs):
        from app.observability.outbox import OutboxUnavailableError

        raise OutboxUnavailableError("sqlite unavailable")

    monkeypatch.setattr(provenance.Outbox, "initialize", _broken)
    from app.config import DatabaseConfig
    monkeypatch.setattr(research, "get_database_config",
                        lambda: DatabaseConfig(host="h", port=5432, name="d", user="u", password="p"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")
    monkeypatch.setattr(research, "reserve_requests",
                        lambda c: {"month": "m", "used": 1, "ceiling": 100, "remaining": 99})
    monkeypatch.setattr(research, "read_budget",
                        lambda: {"month": "m", "used": 0, "ceiling": 100, "remaining": 100})
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [_listing()])
    _patch_pg(monkeypatch, _FakePG(down=True))
    assignments = [a.model_dump() for a in plan_initial_searches()]
    with pytest.raises(DurabilityError):
        research.run_research(research.ResearchRunRequest.model_validate(
            {"assignments": assignments, "include_ranking": True}))


def test_durability_error_maps_to_503(monkeypatch) -> None:
    import app.routes.discovery as routes
    from app.observability.emitter import DurabilityError
    from fastapi import HTTPException

    def _raise(*args, **kwargs):
        raise DurabilityError("no durable store")

    monkeypatch.setattr(routes, "run_research", _raise)
    with pytest.raises(HTTPException) as exc:
        routes.start_research_run(object(), None)
    assert exc.value.status_code == 503
    from app.agents.gabriel.dossier import DossierRequest

    monkeypatch.setattr(routes, "persist_verdicts", _raise)
    body = DossierRequest(rounds=[_intel(_listing())])
    with pytest.raises(HTTPException) as exc:
        routes.create_dossiers(body)
    assert exc.value.status_code == 503


def test_repeated_requests_distinct_runs(monkeypatch, tmp_path) -> None:
    import app.agents.gabriel.research as research
    from app.agents.gabriel.planner import plan_initial_searches

    monkeypatch.setenv("OBS_OUTBOX_PATH", str(tmp_path / "outbox.sqlite3"))
    from app.config import DatabaseConfig
    monkeypatch.setattr(research, "get_database_config",
                        lambda: DatabaseConfig(host="h", port=5432, name="d", user="u", password="p"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")
    monkeypatch.setattr(research, "reserve_requests",
                        lambda c: {"month": "m", "used": 1, "ceiling": 100, "remaining": 99})
    monkeypatch.setattr(research, "read_budget",
                        lambda: {"month": "m", "used": 0, "ceiling": 100, "remaining": 100})
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [_listing()])
    pg = _FakePG()
    _patch_pg(monkeypatch, pg)
    assignments = [a.model_dump() for a in plan_initial_searches()]
    first = research.run_research(research.ResearchRunRequest.model_validate(
        {"assignments": assignments, "include_ranking": True}))
    second = research.run_research(research.ResearchRunRequest.model_validate(
        {"assignments": assignments, "include_ranking": True}))
    assert first.run_id != second.run_id
    rows = _verdict_rows(pg, "gabriel.research.candidate_decision")
    assert {row["run_id"] for row in rows} == {first.run_id, second.run_id}


def test_legacy_exclusion_emission_replaced(monkeypatch, tmp_path) -> None:
    import app.agents.gabriel.research as research
    import app.routes.discovery as routes
    from app.agents.gabriel.planner import plan_initial_searches

    monkeypatch.setenv("OBS_OUTBOX_PATH", str(tmp_path / "outbox.sqlite3"))
    seen: list = []
    monkeypatch.setattr(research, "note_decision",
                        lambda run_id, event_type, summary, **kw: seen.append(event_type),
                        raising=False)
    monkeypatch.setattr(routes, "note_decision",
                        lambda run_id, event_type, summary, **kw: seen.append(event_type),
                        raising=False)
    from app.config import DatabaseConfig
    monkeypatch.setattr(research, "get_database_config",
                        lambda: DatabaseConfig(host="h", port=5432, name="d", user="u", password="p"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")
    monkeypatch.setattr(research, "reserve_requests",
                        lambda c: {"month": "m", "used": 1, "ceiling": 100, "remaining": 99})
    monkeypatch.setattr(research, "read_budget",
                        lambda: {"month": "m", "used": 0, "ceiling": 100, "remaining": 100})
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [_brave_result(
        "Tower demolished",
        "Meridian Tower at 500 Commerce St, Dallas, TX was demolished last year.",
        url="https://example.com/demolished")])
    pg = _FakePG()
    _patch_pg(monkeypatch, pg)
    assignments = [a.model_dump() for a in plan_initial_searches()]
    research.run_research(research.ResearchRunRequest.model_validate(
        {"assignments": assignments, "include_ranking": True}))
    assert "gabriel.ranking.excluded" not in seen
    assert "gabriel.dossier.excluded" not in seen
    rows = _verdict_rows(pg, "gabriel.research.candidate_decision")
    assert len(rows) == 1
    assert rows[0]["details"]["decision"] == "EXCLUDED"


def test_no_provider_or_discovery_calls_in_verdict_flow(monkeypatch) -> None:
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.provenance import build_dossier_verdicts, build_research_verdicts
    from app.agents.gabriel.ranking import rank_intelligence

    def _forbidden(*args, **kwargs):
        raise AssertionError("provider called during verdict flow")

    import app.agents.gabriel.sources.brave as brave
    monkeypatch.setattr(brave, "execute_query", _forbidden)
    intel = _intel(_listing())
    ranking = rank_intelligence(intel)
    assert len(build_research_verdicts(
        run_id="r", correlation_id="", ranking=ranking, intel=intel)) == 1
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    assert len(build_dossier_verdicts(
        run_id="r", correlation_id="", dossier_set=dossier_set, items=intel.results)) == 1


def test_g2_namespaces_disjoint() -> None:
    from app.agents.gabriel.intake import DECISION_EVENT_TYPE as INTAKE_TYPE
    from app.agents.gabriel.service import DECISION_EVENT_TYPE as DISC_TYPE
    from app.agents.gabriel.service import RUN_EVENT_TYPE as RUN_TYPE

    verdict_types = {"gabriel.research.candidate_decision", "gabriel.dossier.verdict"}
    assert verdict_types.isdisjoint({INTAKE_TYPE, DISC_TYPE, RUN_TYPE})


# --- Live PostgreSQL ---


def _live_pg():
    import app.properties_db as pdb

    return pdb.get_connection()


@needs_live_pg
def test_live_verdict_round_trip(tmp_path, monkeypatch) -> None:
    import app.agents.gabriel.research as research
    from app.agents.gabriel.planner import plan_initial_searches
    from app.observability.store import query_events

    monkeypatch.setenv("OBS_OUTBOX_PATH", str(tmp_path / "outbox.sqlite3"))
    from app.config import DatabaseConfig
    monkeypatch.setattr(research, "get_database_config",
                        lambda: DatabaseConfig(host="h", port=5432, name="d", user="u", password="p"))
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")
    monkeypatch.setattr(research, "reserve_requests",
                        lambda c: {"month": "m", "used": 1, "ceiling": 100, "remaining": 99})
    monkeypatch.setattr(research, "read_budget",
                        lambda: {"month": "m", "used": 0, "ceiling": 100, "remaining": 100})
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [_listing()])
    assignments = [a.model_dump() for a in plan_initial_searches()]
    result = None
    try:
        result = research.run_research(research.ResearchRunRequest.model_validate(
            {"assignments": assignments, "include_ranking": True}))
        assert result.provenance_pending is False
        events = query_events(run_id=result.run_id, event_type="gabriel.research.candidate_decision")
        assert len(events) == 1
        assert events[0].details["decision"] == "PASS"
        assert events[0].retention_class == RetentionClass.PERMANENT
    finally:
        if result is not None:
            with _live_pg() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM agent_events WHERE run_id = %(run_id)s",
                                {"run_id": result.run_id})
                conn.commit()


@needs_live_pg
def test_live_outbox_recovery_delivers_verdicts(tmp_path, monkeypatch) -> None:
    from app.agents.gabriel.provenance import build_research_verdicts, persist_verdicts
    from app.agents.gabriel.ranking import rank_intelligence
    from app.observability.outbox import Outbox
    from app.observability.recovery import recover_once
    from app.observability.store import query_events

    intel = _intel(_listing())
    ranking = rank_intelligence(intel)
    run_id = f"live-recovery-{uuid4().hex}"
    verdicts = build_research_verdicts(
        run_id=run_id, correlation_id="", ranking=ranking, intel=intel)
    box = Outbox(tmp_path / "outbox.sqlite3")
    box.initialize()
    box.accept_all([v for v in verdicts])
    try:
        assert query_events(run_id=run_id) == []
        report = recover_once(box, purge_delivered_days=None)
        assert report.delivered == len(verdicts)
        events = query_events(run_id=run_id, event_type="gabriel.research.candidate_decision")
        assert len(events) == len(verdicts)
    finally:
        with _live_pg() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_events WHERE run_id = %(run_id)s",
                            {"run_id": run_id})
            conn.commit()
