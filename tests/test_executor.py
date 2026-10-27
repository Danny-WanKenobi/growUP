"""Investigation executor tests (5B): mocked provider and budget only."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

import app.agents.gabriel.investigation as investigation
from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
from app.agents.gabriel.investigation import (
    InvestigationConfig,
    InvestigationError,
    InvestigationRunRequest,
    plan_investigations,
    run_investigation,
)
from app.agents.gabriel.sources.brave import (
    BraveError,
    BraveRateLimited,
    BraveSearchResult,
    BraveUnauthorized,
)
from app.research_budget_db import BudgetExhausted

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0


def _result(title, snippet, url="https://example.com/story", published_at=None):
    global _RANK
    _RANK += 1
    return BraveSearchResult(
        assignment_id="gsp-exc001",
        query="test query executor",
        title=title,
        url=url,
        snippet=snippet,
        domain=urlparse(url).netloc,
        provider="brave",
        retrieved_at=_RETRIEVED,
        published_at=published_at,
        rank=_RANK,
    )


def _intel(*results):
    return ResearchIntelligence(results=[extract_intelligence(item) for item in results])


def _strong(url="https://example.com/meridian"):
    return _result(
        "Meridian Tower listed for sale",
        "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale with "
        "an asking price of $40 million. The Dallas County office tower drew bids.",
        url=url,
        published_at="2026-10-20",
    )


def _second(url="https://example.com/logport"):
    return _result(
        "Logport warehouse listed",
        "Logport warehouse at 77 Industrial Blvd, Garland, TX is listed for sale. "
        "The Dallas County industrial property seeks buyers.",
        url=url,
    )


def _db(monkeypatch):
    from app.config import DatabaseConfig

    monkeypatch.setattr(
        investigation,
        "get_database_config",
        lambda: DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw"),
    )
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "test-brave-key")
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "100")


def _budget(monkeypatch, ceiling=100, used=0):
    state = {"used": used, "calls": 0}

    def _reserve(count):
        state["calls"] += 1
        if state["used"] + count > ceiling:
            raise BudgetExhausted("budget exhausted")
        state["used"] += count
        return {
            "month": "2026-10",
            "used": state["used"],
            "ceiling": ceiling,
            "remaining": ceiling - state["used"],
        }

    def _read():
        return {
            "month": "2026-10",
            "used": state["used"],
            "ceiling": ceiling,
            "remaining": ceiling - state["used"],
        }

    monkeypatch.setattr(investigation, "reserve_requests", _reserve)
    monkeypatch.setattr(investigation, "read_budget", _read)
    return state


def _provider(monkeypatch, handler):
    calls: list[str] = []

    def _execute(query, assignment_id, timeout):
        calls.append(query)
        return handler(query, assignment_id, timeout)

    monkeypatch.setattr(investigation, "execute_query", _execute)
    return calls


def _followup_result(query_id, query):
    return BraveSearchResult(
        assignment_id=query_id,
        query=query,
        title="Meridian Tower occupancy update",
        url="https://example.com/meridian-update",
        snippet="Meridian Tower at 500 Commerce St, Dallas, TX reports 55% occupancy.",
        domain="example.com",
        provider="brave",
        retrieved_at=_RETRIEVED,
        published_at="2026-10-25",
        rank=1,
    )


def test_valid_plan_executes_bounded_searches(monkeypatch) -> None:
    _db(monkeypatch)
    budget = _budget(monkeypatch)
    calls = _provider(monkeypatch, lambda q, i, t: [_followup_result(i, q)])
    result = run_investigation(InvestigationRunRequest(intelligence=_intel(_strong())))
    assert result.request_count == 2
    assert len(result.executed) == 2
    assert len(result.results) == 2
    assert result.errors == []
    assert result.budget_used == 2
    assert result.budget_remaining == 98
    assert budget["calls"] == 2
    assert len(calls) == 2
    # Every executed query is planner-derived, quoted, and template-shaped.
    for executed in result.executed:
        assert executed.query_id.startswith("gip-")
        assert executed.query.startswith('"Meridian Tower"')
    assert result.intelligence is not None
    assert len(result.intelligence.results) == 2


def test_execution_capped_at_three_queries(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch)
    calls = _provider(monkeypatch, lambda q, i, t: [])
    intel = _intel(_strong(), _second())
    assert sum(len(inv.queries) for inv in plan_investigations(intel).investigations) == 4
    result = run_investigation(InvestigationRunRequest(intelligence=intel))
    assert result.request_count == 3
    assert len(result.executed) == 3
    assert len(calls) == 3


def test_forged_query_ids_rejected_without_provider_call(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch)
    calls = _provider(monkeypatch, lambda q, i, t: [])
    with pytest.raises(InvestigationError):
        run_investigation(
            InvestigationRunRequest(
                intelligence=_intel(_strong()),
                query_ids=["gip-forged000000"],
            )
        )
    assert calls == []


def test_query_id_subset_executes_only_selected(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch)
    calls = _provider(monkeypatch, lambda q, i, t: [])
    intel = _intel(_strong())
    plan = plan_investigations(intel)
    wanted = plan.investigations[0].queries[0].id
    result = run_investigation(
        InvestigationRunRequest(intelligence=intel, query_ids=[wanted])
    )
    assert result.request_count == 1
    assert [item.query_id for item in result.executed] == [wanted]


def test_budget_exhaustion_stops_additional_calls(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch, ceiling=1, used=0)
    calls = _provider(monkeypatch, lambda q, i, t: [])
    result = run_investigation(InvestigationRunRequest(intelligence=_intel(_strong(), _second())))
    assert result.request_count == 1
    assert len(calls) == 1
    codes = sorted(error.error for error in result.errors)
    assert codes == ["budget_exhausted", "not_attempted"]
    assert result.budget_remaining == 0


def test_rate_limit_aborts_remaining(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch)

    def _handler(query, assignment_id, timeout):
        raise BraveRateLimited("429")

    calls = _provider(monkeypatch, _handler)
    result = run_investigation(InvestigationRunRequest(intelligence=_intel(_strong())))
    assert result.request_count == 1  # the attempt still counts
    assert len(calls) == 1
    assert sorted(e.error for e in result.errors) == ["brave_rate_limited", "not_attempted"]


def test_unauthorized_aborts_run(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch)

    def _handler(query, assignment_id, timeout):
        raise BraveUnauthorized("401")

    _provider(monkeypatch, _handler)
    result = run_investigation(InvestigationRunRequest(intelligence=_intel(_strong())))
    assert result.request_count == 1
    assert sorted(e.error for e in result.errors) == ["brave_unauthorized", "not_attempted"]


def test_partial_results_survive_mid_run_failure(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch)
    state = {"n": 0}

    def _handler(query, assignment_id, timeout):
        state["n"] += 1
        if state["n"] == 1:
            return [_followup_result(assignment_id, query)]
        raise BraveError("boom")

    _provider(monkeypatch, _handler)
    result = run_investigation(InvestigationRunRequest(intelligence=_intel(_strong())))
    assert result.request_count == 2
    assert len(result.results) == 1
    assert [e.error for e in result.errors] == ["boom"]
    assert result.intelligence is not None
    assert len(result.intelligence.results) == 1


def test_failed_requests_are_counted_and_reported(monkeypatch) -> None:
    _db(monkeypatch)
    budget = _budget(monkeypatch)

    def _handler(query, assignment_id, timeout):
        raise BraveError("provider down")

    _provider(monkeypatch, _handler)
    result = run_investigation(InvestigationRunRequest(intelligence=_intel(_strong())))
    assert result.request_count == 2
    assert budget["used"] == 2
    assert len(result.errors) == 2
    assert result.results == []


def test_no_secrets_in_output(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch)
    _provider(monkeypatch, lambda q, i, t: [])
    result = run_investigation(InvestigationRunRequest(intelligence=_intel(_strong())))
    assert "test-brave-key" not in result.model_dump_json()


def test_empty_plan_rejected(monkeypatch) -> None:
    _db(monkeypatch)
    _budget(monkeypatch)
    calls = _provider(monkeypatch, lambda q, i, t: [])
    intel = _intel(
        _result(
            "Houston tower listed",
            "Bayou Tower at 500 Smith St, Houston, TX is listed for sale. "
            "The Harris County office tower drew bids.",
            url="https://example.com/bayou",
        )
    )
    with pytest.raises(InvestigationError):
        run_investigation(InvestigationRunRequest(intelligence=intel))
    assert calls == []


def test_missing_database_config_fails_closed(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        investigation, "get_database_config", lambda: SimpleNamespace(is_configured=False)
    )
    from app.db import DatabaseNotConfiguredError

    with pytest.raises(DatabaseNotConfiguredError):
        run_investigation(InvestigationRunRequest(intelligence=_intel(_strong())))


def _api_env(monkeypatch, *, research_key="test-research-key") -> None:
    _db(monkeypatch)
    if research_key is None:
        monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    else:
        monkeypatch.setenv("RESEARCH_API_KEY", research_key)


def test_investigations_endpoint_requires_auth(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    _api_env(monkeypatch)
    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_strong())
    assert (
        client.post(
            "/discovery/investigations",
            json={"intelligence": intel.model_dump(mode="json")},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/discovery/investigations",
            json={"intelligence": intel.model_dump(mode="json")},
            headers={"X-Research-Key": "wrong"},
        ).status_code
        == 401
    )


def test_investigations_endpoint_fails_closed_without_config(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    _api_env(monkeypatch, research_key=None)
    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_strong())
    response = client.post(
        "/discovery/investigations",
        json={"intelligence": intel.model_dump(mode="json")},
        headers={"X-Research-Key": "anything"},
    )
    assert response.status_code == 503


def test_investigations_endpoint_executes_mocked(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    _api_env(monkeypatch)
    _budget(monkeypatch)
    _provider(monkeypatch, lambda q, i, t: [_followup_result(i, q)])
    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_strong())
    response = client.post(
        "/discovery/investigations",
        json={"intelligence": intel.model_dump(mode="json")},
        headers={"X-Research-Key": "test-research-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_count"] == 2
    assert body["budget_remaining"] == 98
    assert body["intelligence"] is not None
    assert "test-brave-key" not in response.text


def test_investigations_endpoint_rejects_forged_ids(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    _api_env(monkeypatch)
    _budget(monkeypatch)
    calls = _provider(monkeypatch, lambda q, i, t: [])
    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_strong())
    response = client.post(
        "/discovery/investigations",
        json={
            "intelligence": intel.model_dump(mode="json"),
            "query_ids": ["gip-forged000000"],
        },
        headers={"X-Research-Key": "test-research-key"},
    )
    assert response.status_code == 422
    assert calls == []


def test_no_direct_database_access(monkeypatch) -> None:
    import app.research_budget_db as budget_module

    _db(monkeypatch)
    _budget(monkeypatch)
    _provider(monkeypatch, lambda q, i, t: [])

    def _forbidden(*args, **kwargs):
        raise AssertionError("executor must not touch the database directly")

    monkeypatch.setattr(budget_module, "get_connection", _forbidden)
    result = run_investigation(InvestigationRunRequest(intelligence=_intel(_strong())))
    assert result.request_count == 2
