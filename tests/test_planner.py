"""Search planner tests (deterministic; no database, no network)."""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents.gabriel.planner import (
    PLANNER_VERSION,
    FeedbackOutcome,
    PlannerConfig,
    ResearchFeedback,
    SearchStrategy,
    plan_followups,
    plan_initial_searches,
)
from app.main import app
from app.properties import PropertyCategory

client = TestClient(app, raise_server_exceptions=False)


def _feedback(parent, outcome: FeedbackOutcome) -> ResearchFeedback:
    return ResearchFeedback(assignment_id=parent.id, outcome=outcome)


def test_default_plan_is_dallas_wide_and_balanced() -> None:
    plan = plan_initial_searches()
    assert len(plan) == 12
    assert all(a.market.endswith("Dallas, TX") for a in plan)
    assert sum(1 for a in plan if not a.downtown) == 9  # city-wide dominates
    for category in (
        PropertyCategory.HIGH_RISE,
        PropertyCategory.DOWNTOWN_COMMERCIAL,
        PropertyCategory.WAREHOUSE_INDUSTRIAL,
    ):
        assert sum(1 for a in plan if a.category is category) == 4
    assert all(a.planner_version == PLANNER_VERSION for a in plan)
    assert [a.priority for a in plan] == list(range(1, 13))


def test_all_strategies_covered_with_full_budget() -> None:
    plan = plan_initial_searches(PlannerConfig(total_max_queries=27, per_category_max=9))
    assert {a.strategy for a in plan} == set(SearchStrategy)
    assert len(plan) == 27


def test_assignments_seek_sale_opportunities() -> None:
    plan = plan_initial_searches(PlannerConfig(total_max_queries=54, per_category_max=18))
    assert len(plan) == 54
    for assignment in plan:
        assert "sale" in assignment.query or "listing" in assignment.query


def test_plan_is_repeatable_with_stable_ids() -> None:
    first = plan_initial_searches()
    second = plan_initial_searches()
    assert [a.id for a in first] == [a.id for a in second]
    assert [a.query for a in first] == [a.query for a in second]
    assert len({a.id for a in first}) == len(first)
    assert all(a.id.startswith("gsp-") for a in first)


def test_value_and_distress_focus() -> None:
    plan = plan_initial_searches()
    by_strategy = {a.strategy: a for a in plan}
    assert SearchStrategy.HIGH_VALUE in by_strategy
    assert "for sale" in by_strategy[SearchStrategy.HIGH_VALUE].query
    assert SearchStrategy.DISTRESSED in by_strategy
    assert "not evidence" in by_strategy[SearchStrategy.DISTRESSED].intent
    assert "not the query" in by_strategy[SearchStrategy.FORECLOSURE_REO].intent
    assert "not proof of distress" in by_strategy[SearchStrategy.LONG_LISTING].intent


def test_downtown_is_secondary_and_optional() -> None:
    plan = plan_initial_searches()
    downtown = [a for a in plan if a.downtown]
    assert 0 < len(downtown) < len(plan) / 2
    assert all(a.market.startswith("Downtown ") for a in downtown)
    citywide = plan_initial_searches(PlannerConfig(downtown_refinement=False))
    assert all(not a.downtown for a in citywide)


def test_configurable_limits_respected() -> None:
    plan = plan_initial_searches(PlannerConfig(total_max_queries=5, per_category_max=4))
    assert len(plan) == 5
    counts = [
        sum(1 for a in plan if a.category is category)
        for category in (
            PropertyCategory.HIGH_RISE,
            PropertyCategory.DOWNTOWN_COMMERCIAL,
            PropertyCategory.WAREHOUSE_INDUSTRIAL,
        )
    ]
    assert max(counts) - min(counts) <= 1  # truncation stays balanced
    assert len(plan_initial_searches(PlannerConfig(total_max_queries=1, per_category_max=1))) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_max_queries": 0},
        {"total_max_queries": 999},
        {"per_category_max": 0},
        {"per_category_max": 999},
        {"followup_max": -1},
        {"max_depth": -1},
        {"max_depth": 99},
        {"city": ""},
    ],
)
def test_invalid_limits_rejected(kwargs) -> None:
    with pytest.raises(ValidationError):
        PlannerConfig(**kwargs)


def test_no_duplicate_queries_at_high_budgets() -> None:
    plan = plan_initial_searches(PlannerConfig(total_max_queries=54, per_category_max=18))
    keys = [(a.query, a.category) for a in plan]
    assert len(set(keys)) == len(keys)


def test_followups_for_each_outcome() -> None:
    parent = plan_initial_searches()[0]  # for_sale, city-wide, depth 0
    assert plan_followups(parent, _feedback(parent, FeedbackOutcome.NO_RESULTS))[0].strategy is SearchStrategy.HIGH_VALUE
    dup = plan_followups(parent, _feedback(parent, FeedbackOutcome.DUPLICATES_HEAVY))
    assert len(dup) == 1 and dup[0].downtown is True
    assert dup[0].strategy is parent.strategy
    promising = plan_followups(parent, _feedback(parent, FeedbackOutcome.PROMISING))
    assert len(promising) == 2
    assert promising[0].parent_id == parent.id
    assert promising[0].depth == 1
    assert plan_followups(parent, _feedback(parent, FeedbackOutcome.PROVIDER_FAILURE)) == []
    pricing = plan_followups(parent, _feedback(parent, FeedbackOutcome.MISSING_PRICING))
    assert pricing[0].strategy is SearchStrategy.HIGH_VALUE
    distress = plan_followups(parent, _feedback(parent, FeedbackOutcome.MISSING_DISTRESS_EVIDENCE))
    assert distress[0].strategy is SearchStrategy.DISTRESSED


def test_followups_respect_depth_followup_and_known_caps() -> None:
    parent = plan_initial_searches()[0]
    deep = parent.model_copy(update={"depth": 2})
    assert plan_followups(deep, _feedback(deep, FeedbackOutcome.PROMISING)) == []
    one = plan_followups(
        parent, _feedback(parent, FeedbackOutcome.PROMISING), PlannerConfig(followup_max=1)
    )
    assert len(one) == 1
    key = f"{parent.query}\x00{parent.category.value}"
    assert plan_followups(parent, _feedback(parent, FeedbackOutcome.NO_RESULTS), known={key}) != []
    child = plan_followups(parent, _feedback(parent, FeedbackOutcome.NO_RESULTS))[0]
    child_key = f"{child.query}\x00{child.category.value}"
    assert plan_followups(
        parent, _feedback(parent, FeedbackOutcome.NO_RESULTS), known={child_key}
    ) == []


def test_followups_are_deterministic() -> None:
    parent = plan_initial_searches()[4]
    feedback = _feedback(parent, FeedbackOutcome.PROMISING)
    first = plan_followups(parent, feedback)
    second = plan_followups(parent, feedback)
    assert [a.id for a in first] == [a.id for a in second]
    assert [a.query for a in first] == [a.query for a in second]


def test_planner_uses_no_database_or_network(monkeypatch) -> None:
    import app.agents.gabriel.planner as planner_module

    def _no_socket(*args, **kwargs):
        raise AssertionError("planner must not open sockets")

    monkeypatch.setattr(socket, "socket", _no_socket)
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    plan = plan_initial_searches()
    assert len(plan) == 12
    parent = next(a for a in plan if not a.downtown)
    assert plan_followups(parent, _feedback(parent, FeedbackOutcome.PROMISING))
    source = open(planner_module.__file__, encoding="utf-8").read()
    for banned in ("properties_db", "listings_db", "httpx", "urllib", "socket", "psycopg"):
        assert banned not in source


def test_search_plan_preview_endpoint() -> None:
    response = client.get("/discovery/search-plan")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 12
    assert body[0]["planner_version"] == PLANNER_VERSION
    assert {"id", "query", "market", "category", "strategy", "priority", "intent"} <= set(body[0])
    custom = client.get("/discovery/search-plan?total=3&per_category=1&downtown=false")
    assert custom.status_code == 200
    assert len(custom.json()) == 3
    assert all(not item["downtown"] for item in custom.json())


def test_search_plan_rejects_invalid_budgets() -> None:
    assert client.get("/discovery/search-plan?total=0").status_code == 422
    assert client.get("/discovery/search-plan?total=9999").status_code == 422
    assert client.get("/discovery/search-plan?per_category=0").status_code == 422
    response = client.get("/discovery/search-plan?total=9999")
    assert response.json() == {"detail": "invalid_plan_config"}
