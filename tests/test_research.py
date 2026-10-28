"""Brave research tests (mocked HTTP and database; no live Brave calls)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.agents.gabriel.research as research
from app.agents.gabriel.planner import plan_initial_searches
from app.agents.gabriel.sources.brave import (
    BraveConfigError,
    BraveError,
    BraveRateLimited,
    BraveTimeout,
    BraveUnauthorized,
    execute_query,
    get_brave_api_key,
    parse_results,
)
from app.main import app
from app.research_budget_db import (
    BudgetConfigError,
    BudgetExhausted,
    get_monthly_ceiling,
    reserve_requests,
)

client = TestClient(app, raise_server_exceptions=False)

_TEST_BRAVE_KEY = "unit-test-brave-key-only"
_TEST_RESEARCH_KEY = "unit-test-research-key-only"

_BRAVE_JSON = {
    "type": "search",
    "web": {
        "results": [
            {
                "title": "Test Tower Listed",
                "url": "https://example.com/tower",
                "description": "Downtown tower for sale.",
                "age": "2026-10-20",
            },
            {"title": "No URL entry", "description": "dropped"},
            "junk-item",
            {"url": "https://example.com/bare"},
        ]
    },
}


def _env(monkeypatch, *, brave_key=_TEST_BRAVE_KEY, research_key=_TEST_RESEARCH_KEY,
         ceiling="100", database=True) -> None:
    if brave_key is None:
        monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    else:
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", brave_key)
    if research_key is None:
        monkeypatch.delenv("RESEARCH_API_KEY", raising=False)
    else:
        monkeypatch.setenv("RESEARCH_API_KEY", research_key)
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", ceiling)
    for var, value in (("DATABASE_HOST", "localhost"), ("DATABASE_PORT", "5432"),
                       ("DATABASE_NAME", "db"), ("DATABASE_USER", "u")):
        if database:
            monkeypatch.setenv(var, value)
        else:
            monkeypatch.delenv(var, raising=False)
    if database:
        monkeypatch.setenv("DATABASE_PASSWORD", "pw")
    else:
        monkeypatch.delenv("DATABASE_PASSWORD", raising=False)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, bad_json=False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("nope")
        return self._payload


# --- Brave client ---


def test_brave_uses_documented_endpoint_and_header(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    _env(monkeypatch)
    seen: dict = {}

    def _get(url, params=None, headers=None, timeout=None):
        seen.update({"url": url, "params": params, "headers": headers, "timeout": timeout})
        return _FakeResponse(payload={"web": {"results": []}})

    monkeypatch.setattr(httpx, "get", _get)
    assert execute_query("tower for sale Dallas", "gsp-1", 12.0) == []
    assert seen["url"] == "https://api.search.brave.com/res/v1/web/search"
    assert seen["params"]["q"] == "tower for sale Dallas"
    assert seen["params"]["count"] == 10
    assert seen["headers"]["X-Subscription-Token"] == _TEST_BRAVE_KEY
    assert seen["timeout"] == 12.0


def test_brave_missing_key_fails_before_http(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    _env(monkeypatch, brave_key=None)

    def _must_not_call(*args, **kwargs):
        raise AssertionError("no request without credentials")

    monkeypatch.setattr(httpx, "get", _must_not_call)
    with pytest.raises(BraveConfigError):
        execute_query("x", "gsp-1", 5.0)
    with pytest.raises(BraveConfigError):
        get_brave_api_key()


def test_brave_maps_provider_failures(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    _env(monkeypatch)

    def _run(handler):
        monkeypatch.setattr(httpx, "get", handler)
        return execute_query("x", "gsp-1", 5.0)

    def _timeout(*a, **k):
        raise httpx.TimeoutException("slow")

    with pytest.raises(BraveTimeout):
        _run(_timeout)

    def _down(*a, **k):
        raise httpx.ConnectError("down")

    with pytest.raises(BraveError):
        _run(_down)
    with pytest.raises(BraveRateLimited):
        _run(lambda *a, **k: _FakeResponse(status_code=429))
    with pytest.raises(BraveUnauthorized):
        _run(lambda *a, **k: _FakeResponse(status_code=401))
    with pytest.raises(BraveError):
        _run(lambda *a, **k: _FakeResponse(status_code=500))
    with pytest.raises(BraveError):
        _run(lambda *a, **k: _FakeResponse(bad_json=True))
    with pytest.raises(BraveError):
        _run(lambda *a, **k: _FakeResponse(payload={"oops": True}))
    with pytest.raises(BraveError):
        _run(lambda *a, **k: _FakeResponse(payload={"web": {"results": "junk"}}))


def test_brave_normalizes_results_and_skips_unattributable() -> None:
    results = parse_results(_BRAVE_JSON, assignment_id="gsp-9", query="q", retrieved_at="2026-10-27T00:00:00+00:00")
    assert len(results) == 2
    first, second = results
    assert (first.title, first.url, first.snippet) == ("Test Tower Listed", "https://example.com/tower", "Downtown tower for sale.")
    assert first.domain == "example.com"
    assert first.provider == "brave"
    assert first.published_at == "2026-10-20"
    assert first.rank == 1
    assert (second.title, second.snippet, second.published_at) == ("", "", None)
    assert second.rank == 4  # provider-side position preserved
    assert parse_results({"web": {}}, assignment_id="a", query="q", retrieved_at="t") == []


def test_brave_errors_leak_no_secrets(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    _env(monkeypatch)

    def _down(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", _down)
    try:
        execute_query("x", "gsp-1", 5.0)
        raise AssertionError("must raise")
    except BraveError as exc:
        assert _TEST_BRAVE_KEY not in str(exc)


# --- Budget accounting ---


class _RecorderCursor:
    def __init__(self, recorder: dict, row) -> None:
        self._recorder = recorder
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def execute(self, query, params=None) -> None:
        self._recorder.setdefault("queries", []).append(query)
        self._recorder["params"] = params

    def fetchone(self):
        return self._row


class _RecorderConnection:
    def __init__(self, recorder: dict, row) -> None:
        self._recorder = recorder
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def cursor(self):
        return _RecorderCursor(self._recorder, self._row)

    def commit(self) -> None:
        self._recorder["committed"] = True

    def rollback(self) -> None:
        self._recorder["rolled_back"] = True


class _BudgetTable:
    """Stateful fake of research_budget (one row per month)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.lock = threading.Lock()

    def connect(self, recorder: dict):
        table = self

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args) -> bool:
                return False

            def execute(self, query, params=None) -> None:
                recorder.setdefault("queries", []).append(query)
                with table.lock:
                    if query.startswith("INSERT INTO research_budget"):
                        table.rows.setdefault(params["month"], {"used": 0, "ceiling": params["ceiling"]})
                        recorder["row"] = None
                    else:
                        row = table.rows[params["month"]]
                        if row["used"] + params["count"] <= params["ceiling"]:
                            row["used"] += params["count"]
                            recorder["row"] = {"used": row["used"]}
                        else:
                            recorder["row"] = None

            def fetchone(self):
                return recorder.get("row")

        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args) -> bool:
                return False

            def cursor(self):
                return _Cursor()

            def commit(self) -> None:
                recorder["committed"] = True

            def rollback(self) -> None:
                recorder["rolled_back"] = True

        return _Connection()


def _db_config():
    from app.config import DatabaseConfig

    return DatabaseConfig(host="h", port=5432, name="db", user="u", password="pw")


def test_ceiling_defaults_and_validates(monkeypatch) -> None:
    monkeypatch.delenv("BRAVE_MONTHLY_CEILING", raising=False)
    assert get_monthly_ceiling() == 100
    monkeypatch.setenv("BRAVE_MONTHLY_CEILING", "25")
    assert get_monthly_ceiling() == 25
    for bad in ("nope", "0", "-3", ""):
        monkeypatch.setenv("BRAVE_MONTHLY_CEILING", bad)
        with pytest.raises(BudgetConfigError):
            get_monthly_ceiling()


def test_reserve_uses_atomic_conditional_update(monkeypatch) -> None:
    psycopg = pytest.importorskip("psycopg")
    _env(monkeypatch)
    recorder: dict = {}
    monkeypatch.setattr(
        psycopg, "connect", lambda **kwargs: _RecorderConnection(recorder, {"used": 7})
    )
    outcome = reserve_requests(3, config=_db_config())
    assert outcome == {"used": 7, "ceiling": 100, "remaining": 93, "month": outcome["month"]}
    assert "ON CONFLICT (research_month) DO NOTHING" in recorder["queries"][0]
    assert "used + %(count)s <= %(ceiling)s" in recorder["queries"][1]
    assert "RETURNING used" in recorder["queries"][1]
    assert recorder.get("committed") is True
    with pytest.raises(ValueError):
        reserve_requests(0)


def test_reserve_exhaustion_rolls_back(monkeypatch) -> None:
    psycopg = pytest.importorskip("psycopg")
    _env(monkeypatch)
    recorder: dict = {}
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: _RecorderConnection(recorder, None))
    with pytest.raises(BudgetExhausted):
        reserve_requests(3, config=_db_config())
    assert recorder.get("rolled_back") is True


def test_budget_accumulates_and_persists_across_connections(monkeypatch) -> None:
    psycopg = pytest.importorskip("psycopg")
    _env(monkeypatch, ceiling="5")
    table = _BudgetTable()
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: table.connect({}))
    assert reserve_requests(3, config=_db_config())["remaining"] == 2
    assert reserve_requests(2, config=_db_config())["remaining"] == 0
    with pytest.raises(BudgetExhausted):
        reserve_requests(1, config=_db_config())


def test_concurrent_reservations_never_overspend(monkeypatch) -> None:
    psycopg = pytest.importorskip("psycopg")
    _env(monkeypatch, ceiling="10")
    table = _BudgetTable()
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: table.connect({}))
    outcomes: list = []

    def _worker():
        try:
            reserve_requests(3, config=_db_config())
            outcomes.append("ok")
        except BudgetExhausted:
            outcomes.append("denied")

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("ok") == 3
    assert outcomes.count("denied") == 3
    assert sum(row["used"] for row in table.rows.values()) == 9


# --- Research execution ---


def _plan_assignments():
    return [a.model_dump() for a in plan_initial_searches()]


def test_selection_picks_first_per_category_in_priority_order() -> None:
    from app.agents.gabriel.planner import SearchAssignment

    selected = research.select_assignments([SearchAssignment.model_validate(a) for a in _plan_assignments()])
    assert len(selected) == 3
    assert [a.priority for a in selected] == sorted(a.priority for a in selected)
    assert len({a.category for a in selected}) == 3


def test_selection_rotation_zero_matches_legacy() -> None:
    from app.agents.gabriel.planner import SearchAssignment

    assignments = [SearchAssignment.model_validate(a) for a in _plan_assignments()]
    assert research.select_assignments(assignments, 0) == research.select_assignments(assignments)


def test_selection_rotation_picks_nth_per_category() -> None:
    from app.agents.gabriel.planner import SearchAssignment

    assignments = [SearchAssignment.model_validate(a) for a in _plan_assignments()]
    first = research.select_assignments(assignments, 0)
    second = research.select_assignments(assignments, 1)
    assert len(second) == 3
    assert len({a.category for a in second}) == 3
    assert [a.priority for a in second] == sorted(a.priority for a in second)
    assert {a.id for a in first} != {a.id for a in second}


def test_rotations_sweep_all_counties() -> None:
    from app.agents.gabriel.geography import ordered_county_names
    from app.agents.gabriel.planner import SearchAssignment

    assignments = [SearchAssignment.model_validate(a) for a in _plan_assignments()]
    assert len(assignments) == 30
    covered = []
    for rotation in range(10):
        selected = research.select_assignments(assignments, rotation)
        assert len(selected) == 3  # per-run Brave limit holds every rotation
        assert {a.county for a in selected} == {ordered_county_names()[rotation]}
        assert len({a.category for a in selected}) == 3
        covered.append(selected[0].county)
    assert covered == list(ordered_county_names())  # exact order, no duplication


def test_selection_rejects_negative_rotation() -> None:
    from app.agents.gabriel.planner import SearchAssignment

    assignments = [SearchAssignment.model_validate(a) for a in _plan_assignments()]
    with pytest.raises(research.ResearchError):
        research.select_assignments(assignments, -1)
    with pytest.raises(ValidationError):
        research.ResearchRunRequest.model_validate(
            {"assignments": _plan_assignments(), "rotation": -1}
        )


def test_selection_rejects_non_planner_input() -> None:
    from app.agents.gabriel.planner import SearchAssignment

    bad = _plan_assignments()[0]
    bad["planner_version"] = "forged-9.9"
    with pytest.raises(research.ResearchError):
        research.select_assignments([SearchAssignment.model_validate(bad)])


# --- County-batch selection hardening ---


def _validated_plan():
    from app.agents.gabriel.planner import SearchAssignment

    return [SearchAssignment.model_validate(a) for a in _plan_assignments()]


def test_selection_rotation_ten_and_beyond_rejected() -> None:
    assignments = _validated_plan()
    for rotation in (10, 11, 99):
        with pytest.raises(research.ResearchError, match="out of range"):
            research.select_assignments(assignments, rotation)


def test_research_endpoint_rejects_rotation_ten(monkeypatch) -> None:
    _env(monkeypatch)
    response = client.post(
        "/discovery/research-runs",
        json={"assignments": _plan_assignments(), "rotation": 10},
        headers={"X-Research-Key": _TEST_RESEARCH_KEY},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_research_request"}


def test_selection_rejects_duplicate_ids() -> None:
    assignments = _validated_plan()
    assignments[1] = assignments[1].model_copy(update={"id": assignments[0].id})
    with pytest.raises(research.ResearchError, match="duplicate assignment ids"):
        research.select_assignments(assignments)


def test_selection_rejects_duplicate_priorities() -> None:
    assignments = _validated_plan()
    assignments[1] = assignments[1].model_copy(update={"priority": 1})
    with pytest.raises(research.ResearchError, match="duplicate priorities"):
        research.select_assignments(assignments)


def test_selection_rejects_priority_gap() -> None:
    assignments = _validated_plan()
    assignments[5] = assignments[5].model_copy(update={"priority": 99})
    with pytest.raises(research.ResearchError, match="no gaps"):
        research.select_assignments(assignments)


def test_selection_rejects_partial_trailing_triple() -> None:
    assignments = _validated_plan()[:29]
    with pytest.raises(research.ResearchError, match="partial triple"):
        research.select_assignments(assignments)


def test_selection_rejects_mixed_county_triple() -> None:
    assignments = _validated_plan()
    # Swap priorities across the Dallas/Tarrant boundary: triple 0
    # becomes Dallas, Dallas, Tarrant.
    dallas_last = assignments[2].model_copy(update={"priority": 4})
    tarrant_first = assignments[3].model_copy(update={"priority": 3})
    assignments[2], assignments[3] = dallas_last, tarrant_first
    with pytest.raises(research.ResearchError, match="mixed-county"):
        research.select_assignments(assignments)


def test_selection_rejects_scrambled_category_order() -> None:
    assignments = _validated_plan()
    # Swap priorities within the Dallas triple: downtown first.
    first = assignments[0].model_copy(update={"priority": 2})
    second = assignments[1].model_copy(update={"priority": 1})
    assignments[0], assignments[1] = first, second
    with pytest.raises(research.ResearchError, match="in order"):
        research.select_assignments(assignments)


def test_selection_requires_exact_county_display_form() -> None:
    assignments = _validated_plan()
    for index in range(3):
        assignments[index] = assignments[index].model_copy(
            update={"county": "dallas county"}
        )
    # Triple shape survives, but no canonical plan carries the
    # tampered label.
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments, 0)


def test_dallas_only_plan_supports_rotation_zero_only() -> None:
    from app.agents.gabriel.planner import (
        PlannerConfig,
        SearchAssignment,
        plan_initial_searches,
    )

    dumped = [
        a.model_dump()
        for a in plan_initial_searches(
            PlannerConfig(include_secondary_counties=False)
        )
    ]
    only = [SearchAssignment.model_validate(a) for a in dumped]
    assert len(only) == 27
    selected = research.select_assignments(only, 0)
    assert [a.county for a in selected] == ["Dallas County"] * 3
    assert [a.strategy.value for a in selected] == ["for_sale"] * 3
    with pytest.raises(research.ResearchError, match="not covered"):
        research.select_assignments(only, 1)


def test_selection_rejects_noncanonical_multipass_plan() -> None:
    from app.agents.gabriel.planner import (
        PlannerConfig,
        SearchAssignment,
        plan_initial_searches,
    )

    dumped = [
        a.model_dump()
        for a in plan_initial_searches(
            PlannerConfig(total_max_queries=54, per_category_max=18)
        )
    ]
    assignments = [SearchAssignment.model_validate(a) for a in dumped]
    assert len(assignments) == 54
    # Structurally perfect (18 complete triples) yet not an
    # executable canonical plan: strategy passes need client-named
    # configuration to regenerate, so they are rejected outright.
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments, 0)


# --- Canonical plan verification ---


def test_untouched_canonical_baseline_succeeds() -> None:
    assignments = _validated_plan()
    assert len(assignments) == 30
    dallas = research.select_assignments(assignments, 0)
    assert [a.county for a in dallas] == ["Dallas County"] * 3


def test_selection_rejects_substituted_query() -> None:
    assignments = _validated_plan()
    assignments[0] = assignments[0].model_copy(
        update={"query": "arbitrary attacker query buy cheap land now"}
    )
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments)


def test_selection_rejects_substituted_id() -> None:
    assignments = _validated_plan()
    assignments[0] = assignments[0].model_copy(update={"id": "gsp-forged000001"})
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments)


def test_selection_rejects_changed_strategy() -> None:
    from app.agents.gabriel.planner import SearchStrategy

    assignments = _validated_plan()
    assignments[0] = assignments[0].model_copy(
        update={"strategy": SearchStrategy.DISTRESSED}
    )
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments)


def test_selection_rejects_changed_market() -> None:
    assignments = _validated_plan()
    assignments[0] = assignments[0].model_copy(
        update={
            "market": "Austin, TX",
            "query": "high-rise office building for sale in Austin, TX",
        }
    )
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments)


def test_selection_rejects_changed_county() -> None:
    assignments = _validated_plan()
    for index in range(3):
        assignments[index] = assignments[index].model_copy(
            update={"county": "Travis County"}
        )
    # Whole-triple county swap keeps triple shape but names a county
    # no canonical plan covers.
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments, 0)


def test_selection_rejects_extra_assignments() -> None:
    assignments = _validated_plan()
    extras = [
        assignments[index].model_copy(
            update={"id": f"gsp-extra-{index:06d}", "priority": 31 + index}
        )
        for index in range(3)
    ]
    bloated = assignments + extras
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(bloated)


def test_selection_failure_touches_no_budget_or_network(monkeypatch) -> None:
    _env(monkeypatch)

    def _reserve_bomb(n):
        raise AssertionError("no reservation after selection failure")

    def _execute_bomb(*args, **kwargs):
        raise AssertionError("no network after selection failure")

    monkeypatch.setattr(research, "reserve_requests", _reserve_bomb)
    monkeypatch.setattr(research, "execute_query", _execute_bomb)
    good = {"assignments": _plan_assignments()}
    with pytest.raises(research.ResearchError):
        research.run_research(
            research.ResearchRunRequest.model_validate(
                {**good, "rotation": 10}
            )
        )
    scrambled = [dict(a) for a in _plan_assignments()]
    scrambled[0]["priority"], scrambled[1]["priority"] = (
        scrambled[1]["priority"], scrambled[0]["priority"],
    )
    with pytest.raises(research.ResearchError):
        research.run_research(
            research.ResearchRunRequest.model_validate({"assignments": scrambled})
        )
    forged = [dict(a) for a in _plan_assignments()]
    forged[0]["query"] = "arbitrary attacker query buy cheap land now"
    with pytest.raises(research.ResearchError):
        research.run_research(
            research.ResearchRunRequest.model_validate({"assignments": forged})
        )


def test_run_research_success(monkeypatch) -> None:
    _env(monkeypatch)
    calls: list = []

    def _execute(query, assignment_id, timeout):
        from app.agents.gabriel.sources.brave import BraveSearchResult

        calls.append((query, timeout))
        return [
            BraveSearchResult.model_validate(
                {
                    "assignment_id": assignment_id, "query": query, "title": "T",
                    "url": "https://example.com/x", "snippet": "S", "domain": "example.com",
                    "provider": "brave", "retrieved_at": "2026-10-27T00:00:00+00:00",
                    "published_at": None, "rank": 1,
                }
            )
        ]

    monkeypatch.setattr(research, "execute_query", _execute)
    spent = {"used": 0}

    def _reserve(n):
        spent["used"] += n
        return {"used": spent["used"], "ceiling": 100,
                "remaining": 100 - spent["used"], "month": "2026-10"}

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    request = research.ResearchRunRequest.model_validate(
        {"assignments": _plan_assignments(), "timeout_seconds": 10.0}
    )
    result = research.run_research(request)
    assert len(calls) == 3
    assert all(timeout == 10.0 for _, timeout in calls)
    assert result.request_count == 3
    assert result.budget_remaining == 97
    assert len(result.results) == 3
    assert result.errors == []
    assert [e["query"] if isinstance(e, dict) else e.query for e in result.executed]


def test_run_research_honors_rotation(monkeypatch) -> None:
    _env(monkeypatch)
    calls: list = []

    def _execute(query, assignment_id, timeout):
        from app.agents.gabriel.sources.brave import BraveSearchResult

        calls.append(query)
        return [
            BraveSearchResult.model_validate(
                {
                    "assignment_id": assignment_id, "query": query, "title": "T",
                    "url": "https://example.com/x", "snippet": "S", "domain": "example.com",
                    "provider": "brave", "retrieved_at": "2026-10-27T00:00:00+00:00",
                    "published_at": None, "rank": 1,
                }
            )
        ]

    monkeypatch.setattr(research, "execute_query", _execute)
    spent = {"used": 0}

    def _reserve(n):
        spent["used"] += n
        return {"used": spent["used"], "ceiling": 100,
                "remaining": 100 - spent["used"], "month": "2026-10"}

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    first = research.run_research(
        research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    )
    rotated = research.run_research(
        research.ResearchRunRequest.model_validate(
            {"assignments": _plan_assignments(), "rotation": 1}
        )
    )
    assert first.request_count == rotated.request_count == 3
    assert [e.query for e in first.executed] != [e.query for e in rotated.executed]
    assert any("Dallas County" in query for query in calls)
    assert any("Tarrant County" in query for query in calls)


def test_run_research_requires_database_first(monkeypatch) -> None:
    _env(monkeypatch, database=False)

    def _must_not_spend(*args, **kwargs):
        raise AssertionError("no budget spend without database")

    monkeypatch.setattr(research, "reserve_requests", _must_not_spend)
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    with pytest.raises(Exception, match="database_not_configured"):
        research.run_research(request)


def test_run_research_checks_brave_key_before_budget(monkeypatch) -> None:
    _env(monkeypatch, brave_key=None)

    def _must_not_reserve(*args, **kwargs):
        raise AssertionError("no reservation without Brave credentials")

    monkeypatch.setattr(research, "reserve_requests", _must_not_reserve)
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    with pytest.raises(Exception, match="not configured"):
        research.run_research(request)


def test_run_research_continues_past_item_errors(monkeypatch) -> None:
    from app.agents.gabriel.sources.brave import BraveError

    _env(monkeypatch)
    attempts: list = []

    def _flaky(query, assignment_id, timeout):
        attempts.append(assignment_id)
        if len(attempts) == 2:
            raise BraveError("brave_bad_response")
        return []

    monkeypatch.setattr(research, "execute_query", _flaky)
    monkeypatch.setattr(
        research, "reserve_requests",
        lambda n: {"used": n, "ceiling": 100, "remaining": 97, "month": "2026-10"},
    )
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    result = research.run_research(request)
    assert len(attempts) == 3  # failed attempt still counted, run continues
    assert len(result.errors) == 1
    assert result.errors[0].error == "brave_bad_response"
    assert result.request_count == 3


def test_run_research_aborts_on_rate_limit(monkeypatch) -> None:
    from app.agents.gabriel.sources.brave import BraveRateLimited

    _env(monkeypatch)
    attempts: list = []

    def _limited(query, assignment_id, timeout):
        attempts.append(assignment_id)
        raise BraveRateLimited("brave_rate_limited")

    monkeypatch.setattr(research, "execute_query", _limited)
    monkeypatch.setattr(
        research, "reserve_requests",
        lambda n: {"used": n, "ceiling": 100, "remaining": 97, "month": "2026-10"},
    )
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    result = research.run_research(request)
    assert len(attempts) == 1
    assert [e.error for e in result.errors] == ["brave_rate_limited", "not_attempted", "not_attempted"]


def test_run_research_writes_no_properties(monkeypatch) -> None:
    import app.properties_db as store

    _env(monkeypatch)

    def _must_not_write(*args, **kwargs):
        raise AssertionError("research must not touch property records")

    monkeypatch.setattr(store, "create_property", _must_not_write)
    monkeypatch.setattr(store, "enrich_property_estimates", _must_not_write)
    monkeypatch.setattr(research, "execute_query", lambda q, aid, t: [])
    monkeypatch.setattr(
        research, "reserve_requests",
        lambda n: {"used": n, "ceiling": 100, "remaining": 97, "month": "2026-10"},
    )
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    assert research.run_research(request).request_count == 3


# --- Endpoint ---


def test_research_endpoint_auth_and_errors(monkeypatch) -> None:
    body = {"assignments": _plan_assignments()}
    # Missing key configuration fails closed.
    _env(monkeypatch, research_key=None)
    response = client.post("/discovery/research-runs", json=body, headers={"X-Research-Key": "x"})
    assert response.status_code == 503
    # Missing / wrong header rejected.
    _env(monkeypatch)
    assert client.post("/discovery/research-runs", json=body).status_code == 401
    response = client.post(
        "/discovery/research-runs", json=body, headers={"X-Research-Key": "wrong"}
    )
    assert response.status_code == 401
    assert "wrong" not in response.text
    assert _TEST_RESEARCH_KEY not in response.text


def test_research_endpoint_full_stack(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    psycopg = pytest.importorskip("psycopg")
    _env(monkeypatch)
    table = _BudgetTable()
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: table.connect({}))
    seen: dict = {}

    def _get(url, params=None, headers=None, timeout=None):
        seen["token_present"] = bool((headers or {}).get("X-Subscription-Token"))
        return _FakeResponse(payload=_BRAVE_JSON)

    monkeypatch.setattr(httpx, "get", _get)
    response = client.post(
        "/discovery/research-runs",
        json={"assignments": _plan_assignments(), "timeout_seconds": 10.0},
        headers={"X-Research-Key": _TEST_RESEARCH_KEY},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_count"] == 3
    assert body["budget_remaining"] == 97
    assert len(body["executed"]) == 3
    assert len(body["results"]) == 6  # 2 normalized results x 3 queries
    assert body["errors"] == []
    assert seen["token_present"] is True
    assert _TEST_BRAVE_KEY not in response.text
    assert _TEST_RESEARCH_KEY not in response.text


def test_research_endpoint_budget_exhausted(monkeypatch) -> None:
    _env(monkeypatch, ceiling="2")
    calls: list = []
    monkeypatch.setattr(
        research, "execute_query", lambda q, aid, t: calls.append(aid) or []
    )
    import app.research_budget_db as budget

    monkeypatch.setattr(research, "reserve_requests", budget.reserve_requests)
    monkeypatch.setattr(research, "read_budget", budget.read_budget)
    psycopg = pytest.importorskip("psycopg")
    table = _BudgetTable()
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: table.connect({}))
    monkeypatch.setattr(
        "app.research_budget_db.get_connection",
        lambda config=None: table.connect({}),
    )
    response = client.post(
        "/discovery/research-runs",
        json={"assignments": _plan_assignments()},
        headers={"X-Research-Key": _TEST_RESEARCH_KEY},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["request_count"] == 2  # third reservation denied, run stopped
    assert len(calls) == 2
    assert body["budget_remaining"] == 0
    assert [e["error"] for e in body["errors"]] == ["budget_exhausted"]


def test_reserve_one_before_each_request(monkeypatch) -> None:
    _env(monkeypatch)
    events: list = []

    def _reserve(n):
        events.append(f"reserve({n})")
        return {"used": len([e for e in events if e.startswith("reserve")]),
                "ceiling": 100, "remaining": 99, "month": "2026-10"}

    def _execute(query, assignment_id, timeout):
        events.append("execute")
        return []

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    monkeypatch.setattr(research, "execute_query", _execute)
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    result = research.run_research(request)
    assert events == ["reserve(1)", "execute"] * 3
    assert result.request_count == 3


def test_mid_run_exhaustion_stops_immediately(monkeypatch) -> None:
    _env(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        research, "execute_query", lambda q, aid, t: calls.append(aid) or []
    )
    state = {"n": 0}

    def _reserve(n):
        state["n"] += 1
        if state["n"] > 2:
            from app.research_budget_db import BudgetExhausted

            raise BudgetExhausted("empty")
        return {"used": state["n"], "ceiling": 100, "remaining": 100 - state["n"], "month": "2026-10"}

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    result = research.run_research(request)
    assert result.request_count == 2
    assert len(calls) == 2
    assert [e.error for e in result.errors] == ["budget_exhausted"]
    assert (result.budget_used, result.budget_remaining) == (2, 98)


def test_exhaustion_at_start_reports_budget_without_attempts(monkeypatch) -> None:
    from app.research_budget_db import BudgetExhausted

    _env(monkeypatch)

    def _deny(n):
        raise BudgetExhausted("empty")

    monkeypatch.setattr(research, "reserve_requests", _deny)
    monkeypatch.setattr(
        research, "read_budget",
        lambda: {"used": 100, "ceiling": 100, "remaining": 0, "month": "2026-10"},
    )
    monkeypatch.setattr(
        research, "execute_query",
        lambda q, aid, t: (_ for _ in ()).throw(AssertionError("no attempts when exhausted")),
    )
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    result = research.run_research(request)
    assert result.request_count == 0
    assert result.executed == []
    assert [e.error for e in result.errors] == ["budget_exhausted", "not_attempted", "not_attempted"]
    assert (result.budget_used, result.budget_remaining) == (100, 0)


def test_abort_after_auth_failure_consumes_single_reservation(monkeypatch) -> None:
    from app.agents.gabriel.sources.brave import BraveUnauthorized

    _env(monkeypatch)
    reserves: list = []

    def _reserve(n):
        reserves.append(n)
        return {"used": len(reserves), "ceiling": 100, "remaining": 99, "month": "2026-10"}

    def _denied(query, assignment_id, timeout):
        raise BraveUnauthorized("brave_unauthorized")

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    monkeypatch.setattr(research, "execute_query", _denied)
    request = research.ResearchRunRequest.model_validate({"assignments": _plan_assignments()})
    result = research.run_research(request)
    assert reserves == [1]  # skipped assignments consume nothing
    assert result.request_count == 1
    assert [e.error for e in result.errors] == ["brave_unauthorized", "not_attempted", "not_attempted"]


def test_read_budget_reports_without_reserving(monkeypatch) -> None:
    import app.research_budget_db as budget

    psycopg = pytest.importorskip("psycopg")
    _env(monkeypatch)
    recorder: dict = {}
    monkeypatch.setattr(
        psycopg, "connect", lambda **kwargs: _RecorderConnection(recorder, {"used": 41})
    )
    outcome = budget.read_budget(config=_db_config())
    assert outcome == {"used": 41, "ceiling": 100, "remaining": 59, "month": outcome["month"]}
    assert "SELECT used FROM research_budget" in recorder["queries"][0]
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: _RecorderConnection(recorder, None))
    assert budget.read_budget(config=_db_config())["used"] == 0


def test_research_endpoint_rejects_forged_assignments(monkeypatch) -> None:
    _env(monkeypatch)
    bad = _plan_assignments()[0]
    bad["planner_version"] = "forged-9.9"
    response = client.post(
        "/discovery/research-runs",
        json={"assignments": [bad]},
        headers={"X-Research-Key": _TEST_RESEARCH_KEY},
    )
    assert response.status_code == 422


def test_migration_003_adds_budget_table() -> None:
    sql_path = Path(__file__).resolve().parent.parent / "migrations" / "003_research_budget.sql"
    sql = sql_path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS research_budget" in sql
    assert "research_month TEXT PRIMARY KEY" in sql
    assert "ALTER TABLE properties" not in sql
    assert "CREATE TABLE IF NOT EXISTS properties" not in sql
