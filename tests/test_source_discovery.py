"""Worldwide-qualification discovery integration tests (offline).

The DFW catalog is candidate data only: qualification decides, the
curated registry enrolls, and only Dallas has a compatible adapter.
No candidate earns P1 authority from a domain suffix or an LLM.
"""
from __future__ import annotations

import pytest

from app.agents.gabriel.sources.discovery import (
    discover_sources,
    pilot_source_plan,
    qualified_adapter,
)


def test_dallas_selects_registered_adapter_via_registry_enrollment():
    result = discover_sources("dallas county", "high_rise")
    assert result["selected_adapter"] == "dcad_bulk"
    top = result["ranked"][0]
    assert top["name"].startswith("Dallas Central Appraisal District")
    # Domain alone grants nothing (.org); authority is the curated
    # registry enrollment, stated explicitly.
    assert top["p1_eligible"] is False
    assert "registry" in result["authority_basis"].lower()


def test_non_dallas_counties_stay_provisional_without_adapter():
    for county in ("tarrant county", "collin county", "wise county"):
        result = discover_sources(county, "commercial_property")
        assert result["selected_adapter"] is None
        assert result["ranked"], county
        top = result["ranked"][0]
        assert top["status"] == "needs_review"
        assert top["p1_eligible"] is False
        assert top["authority_candidates"] == []
        assert result["blockers"]


def test_no_county_earns_p1_from_domain_alone():
    plan = pilot_source_plan()
    assert len(plan) == 36
    for entry in plan:
        for ranked in entry["ranked"]:
            assert ranked["p1_eligible"] is False


def test_pilot_plan_covers_canonical_counties_and_categories():
    plan = pilot_source_plan()
    counties = {entry["county"] for entry in plan}
    categories = {entry["category"] for entry in plan}
    assert len(counties) == 12
    assert categories == {
        "high_rise", "commercial_property", "warehouse_industrial"}


def test_qualified_adapter_only_for_dallas():
    assert qualified_adapter("dallas county", "warehouse_industrial") \
        == "dcad_bulk"
    assert qualified_adapter("navarro county", "high_rise") is None


def test_unknown_county_refused():
    with pytest.raises(ValueError):
        discover_sources("travis county", "high_rise")


def test_pilot_plan_annotation_matches_discovery():
    from app.pilot import source_note_for

    note = source_note_for("tarrant county", "high_rise")
    assert note["selected_adapter"] is None
    assert note["top_status"] == "needs_review"
    dallas = source_note_for("Dallas County", "commercial_property")
    assert dallas["selected_adapter"] == "dcad_bulk"
