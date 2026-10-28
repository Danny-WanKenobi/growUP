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


def test_default_plan_is_sequential_ten_county() -> None:
    from app.agents.gabriel.geography import ordered_county_names

    plan = plan_initial_searches()
    assert len(plan) == 30
    assert [a.county for a in plan] == [
        county for county in ordered_county_names() for _ in range(3)
    ]
    assert plan[0].county == "Dallas County"
    assert plan[3].county == "Tarrant County"
    for start in range(0, 30, 3):
        triple = plan[start:start + 3]
        assert {a.county for a in triple} == {triple[0].county}
        assert [a.category for a in triple] == [
            PropertyCategory.HIGH_RISE,
            PropertyCategory.DOWNTOWN_COMMERCIAL,
            PropertyCategory.WAREHOUSE_INDUSTRIAL,
        ]
    assert all(a.strategy is SearchStrategy.FOR_SALE for a in plan)
    assert all(not a.downtown for a in plan)  # countywide baseline
    assert all(a.market.endswith(", TX") for a in plan)
    assert all(a.county in a.market for a in plan)
    assert all(a.market in a.query for a in plan)
    assert all(a.planner_version == PLANNER_VERSION for a in plan)
    assert PLANNER_VERSION == "004B-1.2"
    assert [a.priority for a in plan] == list(range(1, 31))


def test_strategy_passes_advance_after_baseline() -> None:
    plan = plan_initial_searches(PlannerConfig(total_max_queries=33, per_category_max=11))
    assert len(plan) == 33
    assert all(a.strategy is SearchStrategy.FOR_SALE for a in plan[:30])
    assert all(a.strategy is SearchStrategy.HIGH_VALUE for a in plan[30:])
    assert [a.county for a in plan[30:]] == ["Dallas County"] * 3


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


def test_baseline_prioritizes_for_sale_with_intact_intents() -> None:
    plan = plan_initial_searches()
    assert all(a.strategy is SearchStrategy.FOR_SALE for a in plan)
    assert all("for sale" in a.query for a in plan)
    assert all(a.intent.startswith("Find commercial properties") for a in plan)
    # Distress-strategy intents survive untouched for follow-up research.
    child = plan_followups(
        plan[0], _feedback(plan[0], FeedbackOutcome.MISSING_DISTRESS_EVIDENCE)
    )[0]
    assert child.strategy is SearchStrategy.DISTRESSED
    assert "not evidence" in child.intent


def test_downtown_is_opt_in_and_dallas_only() -> None:
    plan = plan_initial_searches()
    assert all(not a.downtown for a in plan)
    refined = plan_initial_searches(PlannerConfig(downtown_refinement=True))
    downtown = [a for a in refined if a.downtown]
    assert len(downtown) == 3
    assert {a.county for a in downtown} == {"Dallas County"}
    assert all(a.market == "Downtown Dallas, TX" for a in downtown)
    for assignment in refined:
        if assignment.market.startswith("Downtown "):
            assert "County" not in assignment.market
        else:
            assert assignment.county in assignment.market


def test_downtown_uses_configured_municipality_literally() -> None:
    refined = plan_initial_searches(
        PlannerConfig(downtown_refinement=True, city="Fort Worth")
    )
    downtown = [a for a in refined if a.downtown]
    assert downtown
    assert all(a.market == "Downtown Fort Worth, TX" for a in downtown)
    # The configured city is the operator's assertion for the primary
    # county; the planner never infers municipality-to-county membership.


def test_market_label_rejects_non_dallas_downtown() -> None:
    from app.agents.gabriel.planner import _market_label

    with pytest.raises(ValueError, match="Dallas County only"):
        _market_label("Tarrant County", "TX", True, "Dallas")
    assert _market_label("Tarrant County", "TX", False, "Dallas") == "Tarrant County, TX"


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
    assert len(plan) == 30
    parent = next(a for a in plan if not a.downtown)
    assert plan_followups(parent, _feedback(parent, FeedbackOutcome.PROMISING))
    source = open(planner_module.__file__, encoding="utf-8").read()
    for banned in ("properties_db", "listings_db", "httpx", "urllib", "socket", "psycopg"):
        assert banned not in source


def test_search_plan_preview_endpoint() -> None:
    response = client.get("/discovery/search-plan")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 30
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


# --- Ten-county coverage (ten-county expansion) ---


def _all_counties() -> set:
    from app.agents.gabriel.geography import ordered_county_names

    return set(ordered_county_names())


def test_default_plan_covers_all_ten_counties() -> None:
    plan = plan_initial_searches()
    assert {a.county for a in plan} == _all_counties()
    assert len(plan) == 30
    for assignment in plan:
        assert assignment.county in assignment.market  # countywide baseline
        assert assignment.market in assignment.query


def test_dallas_first_tarrant_second_in_priority_order() -> None:
    from app.agents.gabriel.geography import ordered_county_names

    plan = plan_initial_searches()
    assert [a.county for a in plan[:6]] == ["Dallas County"] * 3 + ["Tarrant County"] * 3
    assert [a.county for a in plan] == [
        county for county in ordered_county_names() for _ in range(3)
    ]
    for category in (
        PropertyCategory.HIGH_RISE,
        PropertyCategory.DOWNTOWN_COMMERCIAL,
        PropertyCategory.WAREHOUSE_INDUSTRIAL,
    ):
        dallas = [
            a for a in plan if a.county == "Dallas County" and a.category is category
        ]
        assert len(dallas) == 1


def test_county_coverage_matches_ranking_territory() -> None:
    from app.agents.gabriel.geography import ordered_county_names
    from app.agents.gabriel.ranking import SECONDARY_COUNTIES

    # Planner sequencing derives from the shared constants: no drift possible.
    names = ordered_county_names()
    assert len(names) == 10
    assert names[0] == "Dallas County"
    assert {name.lower() for name in names[1:]} == set(SECONDARY_COUNTIES)
    plan = plan_initial_searches(PlannerConfig(total_max_queries=30, per_category_max=10))
    assert {a.county for a in plan} == _all_counties()


def test_dallas_only_mode_available() -> None:
    from app.agents.gabriel.geography import PRIMARY_COUNTY_DISPLAY

    plan = plan_initial_searches(PlannerConfig(include_secondary_counties=False))
    assert len(plan) == 27  # full Dallas query space (3 x 9), then exhausted
    assert {a.county for a in plan} == {PRIMARY_COUNTY_DISPLAY}


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
    assert len(body) == 27
    assert {item["county"] for item in body} == {"Dallas County"}
    default = client.get("/discovery/search-plan")
    assert {item["county"] for item in default.json()} == _all_counties()
