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
    assert all(a.market.endswith(", TX") for a in plan)
    assert sum(1 for a in plan if not a.downtown) == 8  # county-wide dominates
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


# --- Seven-county coverage (MVP-004B-5 integration) ---


def _all_counties() -> set:
    from app.agents.gabriel.geography import (
        PRIMARY_COUNTY_DISPLAY,
        secondary_county_names,
    )

    return {PRIMARY_COUNTY_DISPLAY, *secondary_county_names()}


def test_default_plan_covers_all_seven_counties() -> None:
    plan = plan_initial_searches()
    assert {a.county for a in plan} == _all_counties()
    assert len(plan) == 12  # full coverage at no extra search cost
    for assignment in plan:
        if assignment.downtown and assignment.county == "Dallas County":
            assert assignment.market == "Downtown Dallas, TX"
        else:
            assert assignment.county in assignment.market
        assert assignment.market in assignment.query


def test_dallas_county_holds_primary_priority() -> None:
    from app.agents.gabriel.geography import PRIMARY_COUNTY_DISPLAY

    plan = plan_initial_searches()
    assert plan[0].county == PRIMARY_COUNTY_DISPLAY
    dallas = [a for a in plan if a.county == PRIMARY_COUNTY_DISPLAY]
    assert len(dallas) >= len(plan) // 2
    for category in (
        PropertyCategory.HIGH_RISE,
        PropertyCategory.DOWNTOWN_COMMERCIAL,
        PropertyCategory.WAREHOUSE_INDUSTRIAL,
    ):
        assert any(
            a.category is category and a.county == PRIMARY_COUNTY_DISPLAY for a in plan
        )


def test_county_coverage_matches_ranking_territory() -> None:
    from app.agents.gabriel.geography import secondary_county_names
    from app.agents.gabriel.ranking import SECONDARY_COUNTIES

    # Planner rotation derives from the shared constants: no drift possible.
    assert {name.lower() for name in secondary_county_names()} == set(SECONDARY_COUNTIES)
    plan = plan_initial_searches(PlannerConfig(total_max_queries=24, per_category_max=8))
    assert {a.county for a in plan} == _all_counties()


def test_dallas_only_mode_available() -> None:
    from app.agents.gabriel.geography import PRIMARY_COUNTY_DISPLAY

    plan = plan_initial_searches(PlannerConfig(include_secondary_counties=False))
    assert len(plan) == 12
    assert {a.county for a in plan} == {PRIMARY_COUNTY_DISPLAY}


def test_downtown_secondary_across_counties() -> None:
    plan = plan_initial_searches(PlannerConfig(total_max_queries=24, per_category_max=8))
    downtown = [a for a in plan if a.downtown]
    assert 0 < len(downtown) < len(plan) / 2
    assert all(a.market.startswith("Downtown ") for a in downtown)
    assert len({a.county for a in downtown}) >= 2
    dallas_downtown = [a for a in downtown if a.county == "Dallas County"]
    assert dallas_downtown
    assert all(a.market == "Downtown Dallas, TX" for a in dallas_downtown)


def test_followups_stay_in_parent_county() -> None:
    plan = plan_initial_searches()
    secondary_parent = next(a for a in plan if a.county == "Tarrant County")
    children = plan_followups(secondary_parent, _feedback(secondary_parent, FeedbackOutcome.PROMISING))
    assert children
    assert all(child.county == "Tarrant County" for child in children)
    assert all("Tarrant County" in child.query for child in children)
    legacy = secondary_parent.model_copy(update={"county": ""})
    assert plan_followups(legacy, _feedback(legacy, FeedbackOutcome.NO_RESULTS))[0].county == "Dallas County"


def test_search_plan_endpoint_secondary_toggle() -> None:
    response = client.get("/discovery/search-plan?secondary_counties=false")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 12
    assert {item["county"] for item in body} == {"Dallas County"}
    default = client.get("/discovery/search-plan")
    assert {item["county"] for item in default.json()} == _all_counties()
