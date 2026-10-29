"""B5-1 geographic configuration tests (twelve-county expansion).

Deterministic; no database, no network, no provider calls. Verifies the
approved twelve-county territory, the five-city municipality priority
lists, county context for multi-county municipalities, canonical-plan
versioning, and research rotation bounds.
"""

from __future__ import annotations

import pytest

from app.agents.gabriel import research
from app.agents.gabriel.geography import (
    CITY_PRIORITY_ORDER,
    COUNTY_PRIORITY_ORDER,
    ordered_cities,
    ordered_county_names,
)
from app.agents.gabriel.planner import (
    PLANNED_CATEGORIES,
    PLANNER_VERSION,
    SearchAssignment,
    plan_followups,
    plan_initial_searches,
)
from app.agents.gabriel.planner import FeedbackOutcome, ResearchFeedback
from app.properties import PropertyCategory

_EXPECTED_COUNTIES = (
    "Dallas County",
    "Tarrant County",
    "Collin County",
    "Denton County",
    "Ellis County",
    "Johnson County",
    "Kaufman County",
    "Parker County",
    "Rockwall County",
    "Hunt County",
    "Wise County",
    "Navarro County",
)

_EXPECTED_CITIES = {
    "Dallas County": ("Dallas", "Irving", "Garland", "Mesquite", "Grand Prairie"),
    "Tarrant County": ("Fort Worth", "Arlington", "Mansfield", "Grand Prairie", "North Richland Hills"),
    "Collin County": ("Plano", "McKinney", "Frisco", "Allen", "Wylie"),
    "Denton County": ("Denton", "Lewisville", "Frisco", "Carrollton", "Flower Mound"),
    "Ellis County": ("Waxahachie", "Midlothian", "Ennis", "Red Oak", "Glenn Heights"),
    "Johnson County": ("Burleson", "Cleburne", "Joshua", "Venus", "Alvarado"),
    "Kaufman County": ("Forney", "Terrell", "Kaufman", "Crandall", "Mabank"),
    "Parker County": ("Weatherford", "Willow Park", "Aledo", "Springtown", "Reno"),
    "Rockwall County": ("Rockwall", "Fate", "Royse City", "Heath", "Rowlett"),
    "Hunt County": ("Greenville", "Commerce", "Caddo Mills", "Royse City", "West Tawakoni"),
    "Wise County": ("Decatur", "Bridgeport", "Boyd", "Runaway Bay", "Rhome"),
    "Navarro County": ("Corsicana", "Kerens", "Rice", "Blooming Grove", "Dawson"),
}


def test_twelve_county_order_is_exact() -> None:
    assert ordered_county_names() == _EXPECTED_COUNTIES
    assert len(COUNTY_PRIORITY_ORDER) == 12


def test_five_cities_per_county_in_priority_order() -> None:
    assert set(CITY_PRIORITY_ORDER) == {name.lower() for name in _EXPECTED_COUNTIES}
    for county, cities in _EXPECTED_CITIES.items():
        assert ordered_cities(county) == cities
        assert len(ordered_cities(county)) == 5


def test_ordered_cities_accepts_canonical_form() -> None:
    assert ordered_cities("dallas county") == _EXPECTED_CITIES["Dallas County"]
    assert ordered_cities("Navarro County") == _EXPECTED_CITIES["Navarro County"]


def test_ordered_cities_rejects_unknown_county() -> None:
    with pytest.raises(KeyError):
        ordered_cities("Harris County")


def test_multi_county_municipalities_keep_county_context() -> None:
    assert "Grand Prairie" in ordered_cities("Dallas County")
    assert "Grand Prairie" in ordered_cities("Tarrant County")
    assert ordered_cities("Dallas County") != ordered_cities("Tarrant County")
    assert "Frisco" in ordered_cities("Collin County")
    assert "Frisco" in ordered_cities("Denton County")
    assert "Royse City" in ordered_cities("Rockwall County")
    assert "Royse City" in ordered_cities("Hunt County")
    # Same city, different county list positions: the list, not the bare
    # name, carries the county context.
    assert ordered_cities("Dallas County").index("Grand Prairie") == 4
    assert ordered_cities("Tarrant County").index("Grand Prairie") == 3


def test_planned_categories_unchanged() -> None:
    assert PLANNED_CATEGORIES == (
        PropertyCategory.HIGH_RISE,
        PropertyCategory.DOWNTOWN_COMMERCIAL,
        PropertyCategory.WAREHOUSE_INDUSTRIAL,
    )


def test_baseline_assignments_carry_county_city_lists() -> None:
    plan = plan_initial_searches()
    assert len(plan) == 36
    for assignment in plan:
        assert assignment.cities == list(_EXPECTED_CITIES[assignment.county])
    # Baseline queries stay countywide: no city multiplication.
    assert all(assignment.county in assignment.market for assignment in plan)
    assert all(assignment.market in assignment.query for assignment in plan)


def test_followups_carry_parent_county_city_lists() -> None:
    parent = next(a for a in plan_initial_searches() if a.county == "Hunt County")
    children = plan_followups(
        parent,
        ResearchFeedback(assignment_id=parent.id, outcome=FeedbackOutcome.PROMISING),
    )
    assert children
    assert all(child.cities == list(_EXPECTED_CITIES["Hunt County"]) for child in children)


def test_old_planner_version_rejected() -> None:
    assignments = [SearchAssignment.model_validate(a.model_dump()) for a in plan_initial_searches()]
    assignments[0] = assignments[0].model_copy(update={"planner_version": "004B-1.2"})
    with pytest.raises(research.ResearchError, match="unsupported planner version"):
        research.select_assignments(assignments, 0)


def test_stripped_city_lists_rejected_as_noncanonical() -> None:
    assignments = [SearchAssignment.model_validate(a.model_dump()) for a in plan_initial_searches()]
    assignments[0] = assignments[0].model_copy(update={"cities": []})
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments, 0)


def test_tampered_city_lists_rejected_as_noncanonical() -> None:
    assignments = [SearchAssignment.model_validate(a.model_dump()) for a in plan_initial_searches()]
    tampered = list(assignments[0].cities)
    tampered[0] = "Austin"
    assignments[0] = assignments[0].model_copy(update={"cities": tampered})
    with pytest.raises(research.ResearchError, match="canonical plan"):
        research.select_assignments(assignments, 0)


def test_rotation_bounds_cover_twelve_counties() -> None:
    assignments = [SearchAssignment.model_validate(a.model_dump()) for a in plan_initial_searches()]
    covered = []
    for rotation in range(12):
        selected = research.select_assignments(assignments, rotation)
        assert len(selected) == 3
        assert {a.county for a in selected} == {ordered_county_names()[rotation]}
        covered.append(selected[0].county)
    assert covered == list(ordered_county_names())
    assert PLANNER_VERSION == "004B-2.0"


def test_request_model_accepts_36_assignment_baseline() -> None:
    dumped = [a.model_dump() for a in plan_initial_searches()]
    request = research.ResearchRunRequest.model_validate({"assignments": dumped, "rotation": 11})
    assert len(request.assignments) == 36


def test_request_model_rejects_oversized_plan() -> None:
    from pydantic import ValidationError

    dumped = [a.model_dump() for a in plan_initial_searches()]
    with pytest.raises(ValidationError):
        research.ResearchRunRequest.model_validate(
            {"assignments": dumped + dumped[:1], "rotation": 0}
        )
