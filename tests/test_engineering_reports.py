"""Concept report arithmetic and evidence boundaries; pure offline fixtures."""
import copy

import pytest

from app.agents.onesimus.report import (
    build_engineering_report, build_food_forest_concept, rainwater_potential,
)
from app.workflow import digest_payload
from app.workflow_db import WorkflowError


def classified(value, unit="", classification="assumed"):
    return {"value": value, "unit": unit, "classification": classification,
            "provenance": "explicit test scenario assumption"}


def inspection():
    return {"source_dossier_digest": "a" * 64, "observed_occupancy": "ground floor occupied; upstairs unknown"}


def scenario(**overrides):
    base = {"complete": True, "calculation_complete": True, "layout_plausible": True,
            "calculation_version": "test-physical-v1", "missing": [],
            "outputs": {"sites": 100, "water_l_per_year": 4500,
                        "lighting_kwh_per_year": 900},
            "formulas": {"sites": "integer fitted containers"}}
    base.update(overrides)
    return base


def test_report_keeps_alternatives_separate_preserves_inputs_and_does_not_certify_capacity():
    inputs = {"bench": scenario(), "rack3": scenario(outputs={"sites": 300})}
    before = copy.deepcopy(inputs)
    source = inspection()
    source["digest"] = digest_payload(source)
    report = build_engineering_report(inputs, inspection=source)
    assert inputs == before
    assert report["source_inspection_digest"] == source["digest"]
    assert report["status"] == "concept_ready"
    assert report["alternatives_not_additive"] is True
    assert report["alternatives"]["bench"]["bill_of_quantities"][0]["value"] == 100
    assert report["construction_ready"] is False and report["verified_building_capacity"] is False
    assert report["financial_handoff"]["prices_or_financial_results_computed_here"] is False
    assert report["digest"] == digest_payload({k: v for k, v in report.items() if k != "digest"})
    assert report["reflective_finishes"]["floor_mylar_automatically_specified"] is False
    assert report["worker_access"]["safe_for_use_verified"] is False
    assert report["water_options"]["groundwater"]["verified_available"] is False


def test_area_only_calculation_is_not_promoted_to_plausible_layout():
    report = build_engineering_report({"bench": scenario(layout_plausible=None)}, inspection=inspection())
    assert report["status"] == "needs_inputs"
    assert "bench:physical_layout" in report["missing"]
    assert report["alternatives"]["bench"]["calculation_complete"] is True


def test_report_consumes_current_calculator_layout_status():
    from app.agents.onesimus.crops import crop_profile
    from app.agents.onesimus.service import ClassifiedInput, evaluate_scenario
    site = {name: ClassifiedInput(name=name, **classified(value, unit)) for name, value, unit in (
        ("usable_area_m2", 100, "m2"), ("room_length_m", 10, "m"),
        ("room_width_m", 10, "m"), ("clear_height_m", 4, "m"))}
    calculated = evaluate_scenario("bench", crop_profile(), site)
    report = build_engineering_report({"bench": calculated}, inspection=inspection())
    assert report["status"] == "concept_ready"
    assert report["alternatives"]["bench"]["layout_status"] == "plausible_under_assumptions"
    assert any(q["name"] == "root_zone_air_l_total" for q in report["alternatives"]["bench"]["bill_of_quantities"])
    assert report["verified_building_capacity"] is False


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_report_refuses_invalid_physical_quantities(value):
    with pytest.raises(WorkflowError):
        build_engineering_report({"bench": scenario(outputs={"sites": value})}, inspection=inspection())


def test_tampered_inspection_reference_is_refused():
    source = inspection()
    source["digest"] = "f" * 64
    with pytest.raises(WorkflowError, match="inspection report digest"):
        build_engineering_report({"bench": scenario()}, inspection=source)


def test_rain_capture_is_hand_checked_upper_bound_not_savings_or_reliable_supply():
    inputs = {"roof_area_m2": classified(100, "m2"), "rainfall_mm": classified(20, "mm"),
              "runoff_coefficient": classified(.8, "fraction"), "storage_l": classified(1000, "L"),
              "demand_l_for_period": classified(1200, "L"), "period": "event"}
    result = rainwater_potential(inputs)
    assert result["potential_capture_l"] == 1600
    assert result["single_event_storable_l"] == 1000
    assert result["water_offset_upper_bound_l"] == 1000
    assert result["water_bill_savings"] is None
    assert result["reliable_supply_established"] is False
    # A storage tank may turn over repeatedly: do not apply a single-event
    # capacity cap to annual rain and label it dependable annual supply.
    result = rainwater_potential({**inputs, "period": "year"})
    assert result["single_event_storable_l"] is None
    with pytest.raises(WorkflowError):
        rainwater_potential({**inputs, "runoff_coefficient": classified(1.1, "fraction")})


def test_rain_and_lighting_missing_values_are_unknown_and_dli_math_is_explicit():
    report = build_engineering_report({"bench": scenario()}, inspection=inspection(), options={
        "lighting": {"ppfd_umol_m2_s": classified(200, "umol/m2/s"),
                     "photoperiod_h_per_day": classified(16, "h/day")}})
    assert report["lighting"]["electric_dli_mol_m2_day"] == 11.52
    assert report["lighting"]["combined_dli_mol_m2_day"] is None
    assert report["water_options"]["rainwater"]["potential_capture_l"] is None
    assert set(report["water_options"]["rainwater"]["missing"]) == {
        "roof_area_m2", "rainfall_mm", "runoff_coefficient"}
    with pytest.raises(WorkflowError):
        build_engineering_report({}, inspection=inspection(), options={
            "lighting": {"photoperiod_h_per_day": classified(25, "h/day")}})


def test_tenant_up_and_down_options_conserve_observed_occupancy_without_claiming_relocation():
    floors = [{"floor_id": "ground", "level": 0, "area_m2": classified(100, "m2"),
               "occupied_m2": classified(50, "m2")},
              {"floor_id": "upper", "level": 1, "area_m2": classified(100, "m2"),
               "occupied_m2": classified(25, "m2")}]
    report = build_engineering_report({}, inspection=inspection(), options={
        "tenant_consolidation": {"floors": floors}})
    tenant = report["tenant_consolidation"]
    assert tenant["hypothetical"] is True
    assert tenant["floor_inputs"] == floors
    assert tenant["observed_occupancy"] == inspection()["observed_occupancy"]
    up, down = tenant["scenarios"]
    assert up["allocation"][0] == {"floor_id": "upper", "hypothetical_occupied_m2": 75,
                                    "potential_vacant_floor_m2": 25}
    assert down["allocation"][0]["floor_id"] == "ground"
    for option in tenant["scenarios"]:
        assert sum(f["hypothetical_occupied_m2"] for f in option["allocation"]) == 75
    bad = copy.deepcopy(floors)
    bad[0]["occupied_m2"] = classified(101, "m2")
    with pytest.raises(WorkflowError, match="exceeds"):
        build_engineering_report({}, inspection=inspection(), options={
            "tenant_consolidation": {"floors": bad}})


def test_food_forest_requires_provenanced_site_context_and_never_reuses_kratky_yields():
    unknown = build_food_forest_concept({})
    assert unknown["status"] == "needs_inputs"
    assert set(unknown["missing"]) == {"area_m2", "climate", "soil", "water_supply"}
    site = {"area_m2": classified(1000, "m2"), "climate": classified("site climate reference"),
            "soil": classified("soil survey and contamination testing pending", classification="estimated"),
            "water_supply": classified("municipal supply assumption")}
    result = build_food_forest_concept(site)
    assert result["status"] == "site_context_available"
    assert result["yield_estimate"] is None and result["plant_count"] is None
    assert result["profitability"] is None and result["construction_ready"] is False
    assert "cycles_per_year" not in result
    assert result["sources"] == ["owner-food-forest", "forest-farming"]
    site["climate"].pop("provenance")
    assert "climate" in build_food_forest_concept(site)["missing"]
