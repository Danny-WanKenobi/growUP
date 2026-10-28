"""Gabriel instrumentation tests (MVP-004C-A Phase 2).

Event-level assertions monkeypatch the observe helpers in each consuming
module (no sinks touched). A few end-to-end tests enable ambient
observability against tmp log dirs with PG detached. No live Brave, no
live database, no ranking/geography behavior change (full suite guards).
"""

from __future__ import annotations

import json
import os
import stat
from urllib.parse import urlparse
from uuid import uuid4

import pytest

import app.agents.gabriel.intake as intake
import app.agents.gabriel.investigation as investigation
from app.agents.gabriel.dedup import AddressMatch, AddressMatchStatus
import app.agents.gabriel.research as research
import app.observability.emitter as emitter
import app.routes.discovery as discovery_routes
from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
from app.agents.gabriel.planner import plan_initial_searches
from app.observability import (
    ObservabilityConfig,
    RetentionClass,
    reset_sinks,
)

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0


def _tap(monkeypatch, module, *names):
    """Record observe-helper calls as plain dicts."""
    calls: list = []

    def _make(name):
        def _fn(run_id, event_type, summary, **kwargs):
            calls.append(
                {"helper": name, "run_id": run_id, "event_type": event_type,
                 "summary": summary, **kwargs}
            )
            return None

        return _fn

    for name in names:
        monkeypatch.setattr(module, name, _make(name))
    return calls


@pytest.fixture
def obs_tmp(tmp_path, monkeypatch):
    """Enable ambient observability against a tmp log dir, PG detached."""
    monkeypatch.setenv("OBS_ENABLED", "1")
    monkeypatch.setenv("OBS_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OBS_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(emitter, "insert_event", lambda event, config=None: event)
    reset_sinks()
    yield tmp_path
    reset_sinks()


def _result(title, snippet, url="https://example.com/story", published_at=None):
    from app.agents.gabriel.sources.brave import BraveSearchResult

    global _RANK
    _RANK += 1
    return BraveSearchResult(
        assignment_id="gsp-obs001", query="test query", title=title, url=url,
        snippet=snippet, domain=urlparse(url).netloc, provider="brave",
        retrieved_at=_RETRIEVED, published_at=published_at, rank=_RANK,
    )


def _intel(*results):
    return ResearchIntelligence(results=[extract_intelligence(item) for item in results])


def _db_env(monkeypatch):
    from app.config import DatabaseConfig

    monkeypatch.setattr(
        research, "get_database_config",
        lambda: DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw"),
    )
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-brave-key")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")


def _budget(monkeypatch, module, ceiling=100, used=0):
    state = {"used": used}

    def _reserve(count):
        from app.research_budget_db import BudgetExhausted

        if state["used"] + count > ceiling:
            raise BudgetExhausted("exhausted")
        state["used"] += count
        return {"month": "2026-10", "used": state["used"],
                "ceiling": ceiling, "remaining": ceiling - state["used"]}

    def _read():
        return {"month": "2026-10", "used": state["used"],
                "ceiling": ceiling, "remaining": ceiling - state["used"]}

    monkeypatch.setattr(module, "reserve_requests", _reserve)
    monkeypatch.setattr(module, "read_budget", _read)
    return state


# --- Research ---


def test_research_events_and_identity(monkeypatch) -> None:
    _db_env(monkeypatch)
    _budget(monkeypatch, research)
    brave_calls: list = []

    def _execute(query, assignment_id, timeout):
        brave_calls.append(query)
        return []

    monkeypatch.setattr(research, "execute_query", _execute)
    calls = _tap(monkeypatch, research, "note_activity", "note_diagnostic", "note_warning")
    assignments = [a.model_dump() for a in plan_initial_searches()]
    request = research.ResearchRunRequest.model_validate(
        {"assignments": assignments, "correlation_id": "corr-research-1"}
    )
    result = research.run_research(request)
    assert len(brave_calls) == 3  # instrumentation adds no Brave calls
    assert result.run_id
    assert result.correlation_id == "corr-research-1"
    by_type = {call["event_type"]: call for call in calls}
    for expected in ("gabriel.research.started", "gabriel.research.assignments_selected",
                     "gabriel.brave.request_attempted", "gabriel.brave.request_completed",
                     "gabriel.research.results_received", "gabriel.extraction.completed",
                     "gabriel.research.completed"):
        assert expected in by_type
    assert all(call["run_id"] == result.run_id for call in calls)
    assert all(call["correlation_id"] == "corr-research-1" for call in calls)
    selected = by_type["gabriel.research.assignments_selected"]["details"]["selected"]
    assert len(selected) == 3
    assert all("county" in item and "category" in item for item in selected)
    assert by_type["gabriel.research.completed"]["details"]["duration_ms"] >= 0
    extraction = by_type["gabriel.extraction.completed"]["details"]
    assert extraction["results"] == 0 and extraction["property_identities"] == 0


def test_research_ranking_events_and_exclusion_decision(monkeypatch) -> None:
    _db_env(monkeypatch)
    _budget(monkeypatch, research)

    def _execute(query, assignment_id, timeout):
        return [_result(
            "Old mill demolished",
            "The Old Mill at 9 River Rd, Dallas, TX was demolished last year. "
            "The Dallas County site is cleared.",
            url="https://example.com/mill",
        )]

    monkeypatch.setattr(research, "execute_query", _execute)
    calls = _tap(monkeypatch, research, "note_activity", "note_decision")
    assignments = [a.model_dump() for a in plan_initial_searches()]
    request = research.ResearchRunRequest.model_validate(
        {"assignments": assignments, "include_ranking": True}
    )
    result = research.run_research(request)
    assert result.ranking is not None
    by_type = {call["event_type"]: call for call in calls}
    assert "gabriel.ranking.completed" in by_type
    assert by_type["gabriel.ranking.completed"]["details"]["candidates"] == 1
    assert "not investment advice" in by_type["gabriel.ranking.completed"]["summary"]
    excluded = [c for c in calls if c["event_type"] == "gabriel.ranking.excluded"]
    assert len(excluded) == 1
    assert excluded[0]["helper"] == "note_decision"
    assert excluded[0]["details"]["rule"] == "demolished_structure"


def test_research_budget_exhaustion_warns_and_completes(monkeypatch) -> None:
    _db_env(monkeypatch)
    _budget(monkeypatch, research, ceiling=1)
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [])
    calls = _tap(monkeypatch, research, "note_activity", "note_warning")
    assignments = [a.model_dump() for a in plan_initial_searches()]
    result = research.run_research(research.ResearchRunRequest.model_validate({"assignments": assignments}))
    by_type = {call["event_type"]: call for call in calls}
    assert by_type["gabriel.budget.exhausted"]["helper"] == "note_warning"
    assert "gabriel.research.completed" in by_type  # run still completes
    assert result.request_count == 1


def test_research_selection_failure_warns(monkeypatch) -> None:
    from app.agents.gabriel.planner import SearchAssignment

    _db_env(monkeypatch)
    calls = _tap(monkeypatch, research, "note_warning")
    bad = plan_initial_searches()[0].model_dump()
    bad["planner_version"] = "forged-0"
    request = research.ResearchRunRequest.model_validate(
        {"assignments": [SearchAssignment.model_validate(bad).model_dump()]}
    )
    with pytest.raises(research.ResearchError):
        research.run_research(request)
    assert [c["event_type"] for c in calls] == ["gabriel.research.selection_failed"]


# --- Investigation ---


def test_investigation_plan_route_emits(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    calls = _tap(monkeypatch, discovery_routes, "note_activity")
    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_result(
        "Meridian Tower listed",
        "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale with "
        "an asking price of $40 million.",
        url="https://example.com/meridian",
    ))
    response = client.post(
        "/discovery/investigation-plans",
        json={"intelligence": intel.model_dump(mode="json"), "correlation_id": "corr-plan-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] and body["correlation_id"] == "corr-plan-1"
    (created,) = [c for c in calls if c["event_type"] == "gabriel.investigation.plan_created"]
    assert created["run_id"] == body["run_id"]
    assert created["details"]["total_queries"] == body["total_queries"]
    assert created["details"]["properties"] == len(body["investigations"])


def test_investigation_execution_events(monkeypatch) -> None:
    from app.config import DatabaseConfig

    monkeypatch.setattr(
        investigation, "get_database_config",
        lambda: DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw"),
    )
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "k")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")
    _budget(monkeypatch, investigation)
    brave_calls: list = []
    monkeypatch.setattr(
        investigation, "execute_query",
        lambda q, i, t: brave_calls.append(q) or [],
    )
    calls = _tap(monkeypatch, investigation, "note_activity", "note_diagnostic", "note_warning")
    intel = _intel(_result(
        "Meridian Tower listed",
        "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale with "
        "an asking price of $40 million. The Dallas County office tower drew bids.",
        url="https://example.com/meridian",
        published_at="2026-10-20",
    ))
    request = investigation.InvestigationRunRequest.model_validate(
        {"intelligence": intel.model_dump(), "correlation_id": "corr-exec-1"}
    )
    result = investigation.run_investigation(request)
    assert len(brave_calls) == result.request_count  # no extra provider calls
    types = [c["event_type"] for c in calls]
    for expected in ("gabriel.investigation.execution_started",
                     "gabriel.investigation.plan_resolved",
                     "gabriel.brave.request_attempted",
                     "gabriel.brave.request_completed",
                     "gabriel.investigation.completed"):
        assert expected in types
    assert all(c["run_id"] == result.run_id for c in calls)
    completed = next(c for c in calls if c["event_type"] == "gabriel.investigation.completed")
    assert completed["details"]["questions_open"] > 0


# --- Dossier ---


def test_dossier_route_emits_ready_not_sent(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    calls = _tap(monkeypatch, discovery_routes, "note_activity", "note_decision")
    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_result(
        "Meridian Tower listed",
        "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale.",
        url="https://example.com/meridian",
    ))
    response = client.post(
        "/discovery/dossiers",
        json={"rounds": [intel.model_dump(mode="json")]},
    )
    assert response.status_code == 200
    types = [c["event_type"] for c in calls]
    assert "gabriel.dossier.started" in types
    assert "gabriel.dossier.completed" in types
    assert "gabriel.dossier.handoff_ready" in types
    assert not any("handoff_sent" in t for t in types)
    ready = next(c for c in calls if c["event_type"] == "gabriel.dossier.handoff_ready")
    assert "not sent" in ready["summary"]
    assert "Raphael" in ready["details"]["agents"]
    assert all(c["run_id"] == response.json()["run_id"] for c in calls)


def test_dossier_exclusion_is_permanent_decision(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    calls = _tap(monkeypatch, discovery_routes, "note_activity", "note_decision")
    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_result(
        "Old mill demolished",
        "The Old Mill at 9 River Rd, Dallas, TX was demolished last year. "
        "The Dallas County site is cleared.",
        url="https://example.com/mill",
    ))
    response = client.post("/discovery/dossiers", json={"rounds": [intel.model_dump(mode="json")]})
    assert response.status_code == 200
    excluded = [c for c in calls if c["event_type"] == "gabriel.dossier.excluded"]
    assert len(excluded) == 1
    assert excluded[0]["helper"] == "note_decision"


# --- Intake ---


def _intake_body(**overrides):
    body = {
        "name": "Intake Test Building",
        "street_address": "700 Intake Ave",
        "city": "Dallas",
        "state": "TX",
        "zip_code": "75201",
        "category": "downtown_commercial",
        "source_name": "unit-test listing source QA-009",
        "listing_url": "https://example.com/listing/1",
        "observed_at": "2026-10-20T00:00:00+00:00",
        "listing_type": "sale",
    }
    body.update(overrides)
    return body


def _configured(monkeypatch, module):
    from app.config import DatabaseConfig

    monkeypatch.setattr(
        module, "get_database_config",
        lambda: DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw"),
    )


def _txn_env(monkeypatch) -> None:
    class _DummyConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def cursor(self):
            return self

        def execute(self, *args, **kwargs) -> None:
            pass

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

    monkeypatch.setattr(intake, "get_connection", lambda config=None: _DummyConnection())
    monkeypatch.setattr(intake, "acquire_intake_locks", lambda cur, keys: None)
    monkeypatch.setattr(intake, "find_evidence_by_url", lambda url: [])


def test_intake_accepted_event(monkeypatch) -> None:
    _configured(monkeypatch, intake)
    _txn_env(monkeypatch)
    prop_id = str(uuid4())
    monkeypatch.setattr(
        intake, "match_existing_address",
        lambda payload: AddressMatch(AddressMatchStatus.NONE),
    )
    monkeypatch.setattr(intake, "create_property_in_transaction", lambda conn, data: {"id": prop_id})
    monkeypatch.setattr(intake, "create_listing_evidence_in_transaction", lambda conn, **kwargs: {"id": "ev-1"})
    monkeypatch.setattr(intake, "create_distress_observations_in_transaction", lambda conn, pid, rows, **_: [])
    calls = _tap(monkeypatch, intake, "note_activity", "note_warning")
    body = intake.ListingIntakeRequest.model_validate(
        _intake_body(correlation_id="corr-intake-1")
    )
    result = intake.process_intake(body)
    assert result.outcome.value == "created"
    assert result.run_id and result.correlation_id == "corr-intake-1"
    (accepted,) = [c for c in calls if c["event_type"] == "gabriel.intake.accepted"]
    assert accepted["run_id"] == result.run_id
    assert str(accepted["property_id"]) == prop_id
    assert accepted["details"]["outcome"] == "created"
    assert accepted["details"]["listing_evidence_id"] == "ev-1"
    assert accepted["evidence_refs"] == [
        {"kind": "listing_url", "ref": "https://example.com/listing/1"}
    ]
    blob = json.dumps(accepted, default=str)
    assert "700 Intake Ave" not in blob  # no address dump
    assert "Intake Test Building" not in blob  # no name dump


def test_intake_rejected_event(monkeypatch) -> None:
    _configured(monkeypatch, intake)
    _txn_env(monkeypatch)
    monkeypatch.setattr(
        intake, "match_existing_address",
        lambda payload: AddressMatch(AddressMatchStatus.UNIQUE, "22345678-1234-5678-1234-567812345678")
    )
    monkeypatch.setattr(intake, "get_property", lambda pid: None)
    calls = _tap(monkeypatch, intake, "note_activity", "note_warning")
    body = intake.ListingIntakeRequest.model_validate(_intake_body())
    with pytest.raises(intake.IntakeError):
        intake.process_intake(body)
    (rejected,) = [c for c in calls if c["event_type"] == "gabriel.intake.rejected"]
    assert rejected["helper"] == "note_warning"
    assert "duplicate target disappeared" in rejected["details"]["reason"]
    assert "listing_notes" not in json.dumps(rejected)


# --- Retention, redaction, failure behavior ---


def test_decision_helper_selects_permanent_retention(monkeypatch) -> None:
    import app.agents.gabriel.observe as observe

    seen: list = []
    monkeypatch.setattr(observe, "emit_nonfatal", lambda event, **_: seen.append(event) or event)
    observe.note_decision("run-1", "gabriel.test.decision", "decided")
    (event,) = seen
    assert event.retention_class is RetentionClass.PERMANENT
    assert event.agent.value == "gabriel"


def test_note_decision_returns_honest_outcome(obs_tmp) -> None:
    import app.agents.gabriel.observe as observe
    from app.observability import EmitOutcome

    outcome = observe.note_decision("run-1", "gabriel.test.decision", "decided")
    assert isinstance(outcome, EmitOutcome)
    assert outcome.event.retention_class is RetentionClass.PERMANENT
    assert outcome.degraded is False
    assert outcome.persisted_pg is True  # PG detached to no-op in obs_tmp
    assert outcome.file_copy is True


def test_note_decision_degraded_when_disabled(monkeypatch) -> None:
    import app.agents.gabriel.observe as observe

    outcome = observe.note_decision("run-1", "gabriel.test.decision", "decided")
    assert outcome.failed is True  # ambient OBS_ENABLED=0 in tests
    assert outcome.degraded is False
    assert outcome.persisted_pg is False
    assert outcome.error == "observability disabled"


def test_end_to_end_redacted_file_trace(obs_tmp, monkeypatch) -> None:
    _db_env(monkeypatch)
    _budget(monkeypatch, research)
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [])
    assignments = [a.model_dump() for a in plan_initial_searches()]
    result = research.run_research(
        research.ResearchRunRequest.model_validate({"assignments": assignments})
    )
    assert result.run_id
    lines = (obs_tmp / "agent-events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 8
    for line in lines:
        record = json.loads(line)  # every line parses
        assert record["run_id"] == result.run_id
    raw = "\n".join(lines)
    assert "test-brave-key" not in raw
    assert "research.run" in raw or "gabriel.research" in raw


def test_permanent_failure_degrades_without_aborting(obs_tmp, monkeypatch) -> None:
    _db_env(monkeypatch)
    _budget(monkeypatch, research)
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [])

    def _pg_down(event, config=None):
        from app.db import DatabaseUnavailableError

        raise DatabaseUnavailableError("connection_failed")

    monkeypatch.setattr(emitter, "insert_event", _pg_down)
    assignments = [a.model_dump() for a in plan_initial_searches()]
    result = research.run_research(
        research.ResearchRunRequest.model_validate(
            {"assignments": assignments, "include_ranking": True}
        )
    )
    assert result.request_count == 3  # business operation unaffected
    assert result.ranking is not None
    raw = (obs_tmp / "agent-events.jsonl").read_text(encoding="utf-8")
    assert "gabriel.research.completed" in raw  # activity degraded to file


def test_run_ids_unique_across_runs(monkeypatch) -> None:
    _db_env(monkeypatch)
    _budget(monkeypatch, research)
    monkeypatch.setattr(research, "execute_query", lambda q, i, t: [])
    assignments = [a.model_dump() for a in plan_initial_searches()]
    first = research.run_research(
        research.ResearchRunRequest.model_validate({"assignments": assignments})
    )
    second = research.run_research(
        research.ResearchRunRequest.model_validate({"assignments": assignments})
    )
    assert first.run_id and second.run_id and first.run_id != second.run_id


# --- Log file permissions ---


def test_new_and_rotated_files_chmod_640(tmp_path, monkeypatch) -> None:
    import app.observability.jsonlog as jsonlog

    chmod_calls: list = []
    real_chmod = os.chmod

    def _recording_chmod(path, mode):
        chmod_calls.append((str(path), mode))
        return real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", _recording_chmod)
    sink = jsonlog.JsonlSink(
        ObservabilityConfig(
            log_dir=str(tmp_path), log_max_bytes=200, log_backup_count=2
        )
    )
    try:
        base = str(tmp_path / "agent-events.jsonl")
        assert (base, 0o640) in chmod_calls
        for n in range(20):
            sink.write({"severity": "INFO", "n": n, "pad": "x" * 100})
        rotated = [p for p in tmp_path.iterdir() if p.name.startswith("agent-events.jsonl.")]
        assert rotated  # rotation happened
        assert chmod_calls.count((base, 0o640)) >= 2  # fresh base re-chmodded
    finally:
        sink.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_log_file_mode_bits_posix(tmp_path) -> None:
    import app.observability.jsonlog as jsonlog

    sink = jsonlog.JsonlSink(ObservabilityConfig(log_dir=str(tmp_path)))
    try:
        sink.write({"severity": "INFO"})
        mode = stat.S_IMODE(os.stat(tmp_path / "agent-events.jsonl").st_mode)
        assert mode == 0o640
        assert mode & 0o007 == 0  # no world access
        assert mode & 0o020 == 0  # no group write
    finally:
        sink.close()
