"""Pure conceptual engineering reports; no retrieval, authorization or money.

The coordinator attaches this report to an inherited dossier version. References
are a small inspectable knowledge collection, not a trained model or a second
research engine. Site claims retain their evidence status; none of these
calculations establish building capacity, permitted use, or executable designs.
"""
from __future__ import annotations

import copy
import json
import math
from typing import Any

from app.workflow import digest_payload
from app.workflow_db import WorkflowError

REPORT_VERSION = "onesimus-concept-report-1"
CLASSIFICATIONS = {"verified", "estimated", "experimental", "assumed", "unknown"}

SOURCES = (
    {"id": "kratky-primary", "title": "B. A. Kratky: Three non-circulating hydroponic methods for growing lettuce",
     "url": "https://www.ctahr.hawaii.edu/hawaii/downloads/three_non-circulating_hydroponic_methods_for_growing_lettuce.pdf",
     "kind": "primary_research", "scope": "Passive root-zone method and developing airspace; not proof of a building's capacity."},
    {"id": "owner-kratky", "title": "Owner-selected Kratky Hydroponics Guide",
     "url": "https://kratkymethodhydroponics.com/kratky-hydroponics/",
     "kind": "owner_selected_reference", "scope": "Introductory concepts; generalized refill advice must not submerge established air roots."},
    {"id": "lighting-dli", "title": "Virginia Tech: Calculating and Using Daily Light Integral",
     "url": "https://www.pubs.ext.vt.edu/content/pubs_ext_vt_edu/en/SPES/spes-720/spes-720.html",
     "kind": "extension_guidance", "scope": "Crop-level PPFD, photoperiod and daily light integral; crop targets require sourced inputs."},
    {"id": "rainwater", "title": "Texas Water Development Board: Rainwater Harvesting FAQ",
     "url": "https://www.twdb.texas.gov/innovativewater/rainwater/faq.asp",
     "kind": "agency_guidance", "scope": "Roof capture, losses, storage and quality; potential capture is not reliable annual supply."},
    {"id": "groundwater", "title": "Texas Water Development Board: Groundwater and Private Water Well FAQ",
     "url": "https://www.twdb.texas.gov/groundwater/faq/faqgwm.asp",
     "kind": "agency_guidance", "scope": "Site-specific groundwater and district requirements; not an available or free water-source claim."},
    {"id": "owner-food-forest", "title": "Food Forest Abundance",
     "url": "https://foodforestabundance.com/", "kind": "owner_selected_reference",
     "scope": "Layered perennial food-forest concepts; promotional statements are not verified yields or costs."},
    {"id": "forest-farming", "title": "USDA NRCS: Forest Farming",
     "url": "https://www.nrcs.usda.gov/conservation-basics/land/forests/agroforestry-systems/forest-farming",
     "kind": "agency_guidance", "scope": "Multistory planting and site-adapted species; site assessment remains necessary."},
    {"id": "walking-surfaces", "title": "OSHA 1910.22: Walking-working surfaces",
     "url": "https://www.osha.gov/laws-regs/regulations/standardnumber/1910/1910.22",
     "kind": "safety_reference", "scope": "Drainage, surface condition and access are design constraints, not compliance certification."},
    {"id": "scaffolds", "title": "OSHA 1926.451: Scaffolds",
     "url": "https://www.osha.gov/laws-regs/regulations/standardnumber/1926/1926.451",
     "kind": "safety_reference", "scope": "Qualified design, loads and access must be reviewed for the actual equipment and use."},
)

QUANTITY_UNITS = {
    "sites": "count", "containers": "count", "tiers": "count", "growing_m2": "m2",
    "rack_footprint_m2": "m2", "heads_per_year": "heads/year",
    "kg_per_year": "kg/year", "reservoir_l_total": "L",
    "water_l_per_year": "L/year", "nutrient_solution_l_per_year": "L solution/year", "lighting_kwh_per_year": "kWh/year",
    "hvac_kwh_per_year": "kWh/year", "water_plus_structure_kg": "kg",
    "nutrient_l_per_year": "L concentrate/year", "nutrient_concentrate_l_per_year": "L concentrate/year",
    "labor_hours_per_year": "h/year",
    "container_capacity_l_total": "L", "root_zone_air_l_total": "L",
    "lighting_connected_kw": "kW", "dli_mol_m2_day": "mol/m2/day",
}


def _seal(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        json.dumps(payload, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise WorkflowError("engineering report must contain finite JSON values") from exc
    payload["digest"] = digest_payload(payload)
    return payload


def _finite(value: Any, name: str, *, minimum: float = 0.0,
            maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise WorkflowError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise WorkflowError(f"{name} must be a finite number") from exc
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise WorkflowError(f"{name} is outside its physical range")
    return number


def _classified(inputs: dict[str, Any], name: str, missing: list[str],
                *, unit: str | None = None, positive: bool = False,
                maximum: float | None = None) -> float | None:
    entry = inputs.get(name)
    if entry is None or (isinstance(entry, dict) and entry.get("value") is None):
        missing.append(name)
        return None
    if not isinstance(entry, dict):
        raise WorkflowError(f"{name} must carry value, unit, provenance and classification")
    classification = entry.get("classification", "unknown")
    if classification not in CLASSIFICATIONS:
        raise WorkflowError(f"{name} has an invalid classification")
    if classification == "unknown" or not str(entry.get("provenance") or "").strip():
        missing.append(name)
        return None
    if unit is not None and entry.get("unit") != unit:
        raise WorkflowError(f"{name} requires unit {unit}")
    number = _finite(entry.get("value"), name, maximum=maximum)
    if positive and number <= 0:
        raise WorkflowError(f"{name} must be greater than zero")
    return number


def _lighting(options: dict[str, Any]) -> dict[str, Any]:
    inputs = options.get("lighting") or {}
    if not isinstance(inputs, dict):
        raise WorkflowError("lighting options must be an object")
    missing: list[str] = []
    ppfd = _classified(inputs, "ppfd_umol_m2_s", missing, unit="umol/m2/s")
    hours = _classified(inputs, "photoperiod_h_per_day", missing, unit="h/day", maximum=24)
    daylight = _classified(inputs, "daylight_dli_mol_m2_day", missing, unit="mol/m2/day")
    electric_dli = None if ppfd is None or hours is None else ppfd * hours * 3600 / 1_000_000
    return {
        "classification": "conceptual_option", "inputs": copy.deepcopy(inputs),
        "fixture_arrangement": [
            "Mount lower-tier grow lights beneath the shelf above, with crop/fixture clearance checked.",
            "Use ceiling-supported or suspended fixtures for top shelves at the required crop-level light distribution.",
            "Keep fixtures, wiring and mounting outside wet-service hazards; actual ratings and mounting need review.",
        ],
        "electric_dli_mol_m2_day": electric_dli,
        "combined_dli_mol_m2_day": None if electric_dli is None or daylight is None else electric_dli + daylight,
        "formula": "electric_DLI = PPFD_umol_m2_s * photoperiod_h_per_day * 3600 / 1000000",
        "natural_light": {
            "options": ["existing room windows", "roof skylights", "daylight delivery to upper tiers"],
            "estimated_energy_savings": None,
            "constraints": ["seasonal light and rack shading", "glazing/roof suitability and permissions",
                            "heat gain/loss and dehumidification", "crop-specific DLI and uniformity"],
        },
        "missing": sorted(set(missing)), "sources": ["lighting-dli"],
    }


def rainwater_potential(inputs: dict[str, Any] | None) -> dict[str, Any]:
    """Physical capture upper bound for an explicit period, never a water-bill forecast."""
    inputs = inputs or {}
    if not isinstance(inputs, dict):
        raise WorkflowError("rainwater inputs must be an object")
    missing: list[str] = []
    roof = _classified(inputs, "roof_area_m2", missing, unit="m2", positive=True)
    rain = _classified(inputs, "rainfall_mm", missing, unit="mm")
    coefficient = _classified(inputs, "runoff_coefficient", missing, unit="fraction", maximum=1)
    captured = None if any(v is None for v in (roof, rain, coefficient)) else roof * rain * coefficient
    storage = None
    if "storage_l" in inputs:
        storage = _classified(inputs, "storage_l", missing, unit="L", positive=True)
    demand = None
    if "demand_l_for_period" in inputs:
        demand = _classified(inputs, "demand_l_for_period", missing, unit="L")
    period = str(inputs.get("period") or "unspecified period")
    offset = min(captured, demand) if captured is not None and demand is not None else None
    if period == "event" and storage is not None and offset is not None:
        offset = min(offset, storage)
    return {
        "inputs": copy.deepcopy(inputs), "period": period,
        "potential_capture_l": captured,
        "single_event_storable_l": min(captured, storage) if period == "event" and captured is not None and storage is not None else None,
        "water_offset_upper_bound_l": offset,
        "storage_model": "Single-event tank cap only; seasonal turnover and direct use during rainfall are not modeled.",
        "water_bill_savings": None, "reliable_supply_established": False,
        "formula": "potential_capture_L = roof_area_m2 * rainfall_mm_for_period * runoff_coefficient; 1 mm over 1 m2 = 1 L",
        "missing": sorted(set(missing)),
        "constraints": ["rainfall period and seasonal distribution", "first-flush and collection losses",
                        "tank turnover, overflow and dry-period backup", "roof-water quality, treatment and sanitation",
                        "storage footprint, water mass, pumping energy and capital/operating costs"],
        "sources": ["rainwater"],
    }


def _tenant_plan(inspection: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    supplied = options.get("tenant_consolidation") or {}
    if not isinstance(supplied, dict):
        raise WorkflowError("tenant consolidation options must be an object")
    enabled = supplied.get("enabled", True)
    if not isinstance(enabled, bool):
        raise WorkflowError("tenant consolidation enabled must be a boolean")
    floor_inputs = supplied.get("floors") or []
    if not isinstance(floor_inputs, list):
        raise WorkflowError("tenant consolidation floors must be a list")
    floors = []
    missing: list[str] = []
    seen = set()
    for floor in floor_inputs:
        if not isinstance(floor, dict):
            raise WorkflowError("floor inputs must be objects")
        name = str(floor.get("floor_id") or "").strip()
        if not name or name in seen:
            raise WorkflowError("floor ids must be nonempty and unique")
        seen.add(name)
        level = None
        if floor.get("level") is None:
            missing.append(f"floor:{name}:level")
        else:
            level = _finite(floor["level"], "floor level", minimum=-100)
        local_missing: list[str] = []
        area = _classified(floor, "area_m2", local_missing, unit="m2", positive=True)
        occupied = _classified(floor, "occupied_m2", local_missing, unit="m2")
        missing.extend(f"floor:{name}:{field}" for field in local_missing)
        if area is not None and occupied is not None:
            if occupied > area:
                raise WorkflowError("occupied floor area exceeds floor area")
            if level is not None:
                floors.append({"floor_id": name, "level": level, "area_m2": area, "observed_occupied_m2": occupied})
    if not floor_inputs:
        missing.append("floor_by_floor_occupancy_and_area")
    scenarios = []
    if enabled and floors and not missing:
        for direction in ("up", "down"):
            remaining = sum(f["observed_occupied_m2"] for f in floors)
            allocations = []
            for floor in sorted(floors, key=lambda f: f["level"], reverse=direction == "up"):
                relocated = min(remaining, floor["area_m2"])
                remaining -= relocated
                allocations.append({"floor_id": floor["floor_id"], "hypothetical_occupied_m2": relocated,
                                    "potential_vacant_floor_m2": floor["area_m2"] - relocated})
            scenarios.append({"move_tenants": direction, "allocation": allocations})
    return {"hypothetical": True, "enabled": enabled,
            "observed_occupancy": copy.deepcopy(inspection.get("observed_occupancy", "unknown")),
            "floor_inputs": copy.deepcopy(floor_inputs), "scenarios": scenarios,
            "missing": sorted(set(missing)),
            "constraints": ["tenant consent and lease restructuring unestablished",
                            "space interchangeability, access and operational suitability unverified",
                            "relocation and downtime costs unknown",
                            "vacant-floor figures precede agricultural aisles and equipment deductions"]}


def build_food_forest_concept(site: dict[str, Any] | None) -> dict[str, Any]:
    """Site-context concept track, not an indoor-yield model or planting prescription."""
    site = site or {}
    if not isinstance(site, dict):
        raise WorkflowError("food forest site must be an object")
    missing: list[str] = []
    area = _classified(site, "area_m2", missing, unit="m2", positive=True)
    for field in ("climate", "soil", "water_supply"):
        entry = site.get(field)
        if (not isinstance(entry, dict) or entry.get("value") in (None, "")
                or not str(entry.get("provenance") or "").strip()
                or entry.get("classification", "unknown") == "unknown"):
            missing.append(field)
        elif entry.get("classification") not in CLASSIFICATIONS:
            raise WorkflowError(f"food forest {field} has an invalid classification")
    return _seal({
        "schema_version": "onesimus-food-forest-concept-1", "method": "outdoor_layered_perennial_food_forest",
        "status": "needs_inputs" if missing else "site_context_available",
        "conceptual_only": True, "construction_ready": False,
        "site_inputs": copy.deepcopy(site), "site_area_m2": area,
        "planting_layers": ["canopy", "subcanopy", "shrubs", "herbaceous crops", "groundcover", "root crops", "vines"],
        "design_requirements": ["site-adapted species and mature canopy/root spacing",
                                "sun/shade, slope, drainage and access layout",
                                "soil contamination testing and amendment needs",
                                "water availability, retention, irrigation and establishment care",
                                "establishment years, seasonal harvests and maintenance labor"],
        "yield_estimate": None, "profitability": None, "plant_count": None,
        "assumptions": ["Layer roles are concepts; species and planting density require local evidence.",
                        "Perennial establishment and maturity differ from annual lettuce cycles.",
                        "Indoor Kratky water, lighting and crop-cycle formulas are not applied to this track."],
        "missing": sorted(set(missing)), "sources": ["owner-food-forest", "forest-farming"],
    })


def build_engineering_report(scenarios: dict[str, dict[str, Any]], *,
                             inspection: dict[str, Any],
                             options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble alternatives without summing them or promoting site assumptions."""
    if not isinstance(scenarios, dict) or not isinstance(inspection, dict):
        raise WorkflowError("engineering scenarios and inspection must be objects")
    options = options or {}
    if not isinstance(options, dict):
        raise WorkflowError("engineering options must be an object")
    allowed = {"lighting", "rainwater", "tenant_consolidation", "food_forest"}
    if set(options) - allowed:
        raise WorkflowError(f"unsupported engineering options: {sorted(set(options) - allowed)}")
    inspection_digest = digest_payload({k: v for k, v in inspection.items() if k != "digest"})
    if inspection.get("digest") and inspection["digest"] != inspection_digest:
        raise WorkflowError("inspection report digest does not verify")
    alternatives = {}
    missing = []
    for name, scenario in scenarios.items():
        if not isinstance(scenario, dict):
            raise WorkflowError("each engineering scenario must be an object")
        outputs = scenario.get("outputs") or {}
        if not isinstance(outputs, dict):
            raise WorkflowError("engineering outputs must be an object")
        for quantity, value in outputs.items():
            if quantity in QUANTITY_UNITS and value is not None:
                _finite(value, quantity)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                _finite(value, quantity)
        calculation_complete = scenario.get("calculation_complete", scenario.get("complete", False)) is True
        layout = scenario.get("layout") or {}
        plausible = scenario.get("layout_plausible", layout.get("feasible"))
        layout_status = scenario.get("layout_status", layout.get("status"))
        if plausible is None and layout_status:
            plausible = (True if layout_status == "plausible_under_assumptions" else
                         False if layout_status == "impossible" else None)
        concept_ready = calculation_complete and plausible is True
        quantities = []
        supplied_units = scenario.get("output_units") or {}
        for quantity, value in outputs.items():
            if quantity in QUANTITY_UNITS:
                quantities.append({"name": quantity, "value": value,
                                   "unit": supplied_units.get(quantity) or QUANTITY_UNITS[quantity],
                                   "classification": "unknown" if value is None else "estimated",
                                   "provenance": f"deterministic Onesimus calculation {scenario.get('calculation_version', 'unspecified')}"})
        from app.planning_summaries import engineering_budget
        alternatives[str(name)] = {"scenario": copy.deepcopy(scenario),
                                   "calculation_complete": calculation_complete,
                                   "layout_plausible": plausible, "layout_status": layout_status,
                                   "concept_ready": concept_ready,
                                   "bill_of_quantities": quantities, "resource_budget":engineering_budget(scenario)}
        missing.extend(f"{name}:{item}" for item in scenario.get("missing", []))
        missing.extend(f"{name}:{item}" for item in scenario.get("not_quantified", []))
        if plausible is not True:
            missing.append(f"{name}:physical_layout")
    if not scenarios:
        missing.append("authorized_engineering_scenarios")
    authorized = {str(v.get("dossier_digest")) for v in scenarios.values() if v.get("dossier_digest")}
    if len(authorized) > 1:
        raise WorkflowError("engineering scenarios refer to different authorized dossiers")
    report = {
        "schema_version": REPORT_VERSION, "agent": "onesimus", "conceptual_only": True,
        "source_inspection_digest": inspection_digest,
        "source_dossier_digest": next(iter(authorized), None),
        "inspection_input_dossier_digest": inspection.get("source_dossier_digest"),
        "status": "concept_ready" if any(s["concept_ready"] for s in alternatives.values()) else "needs_inputs",
        "construction_ready": False, "verified_building_capacity": False,
        "capacity_status": "not established by this conceptual report",
        "alternatives_not_additive": True, "alternatives": alternatives,
        "lighting": _lighting(options),
        "reflective_finishes": {
            "classification": "conceptual_option", "options": ["Mylar wall lining", "washable reflective wall finish", "durable slip-resistant reflective floor finish"],
            "constraints": ["wet-area cleanability, drainage and slip resistance", "fire/material compatibility and fixture glare", "measured crop-level light benefit"],
            "floor_mylar_automatically_specified": False,
            "energy_savings": None, "sources": ["walking-surfaces", "lighting-dli"],
        },
        "worker_access": {
            "classification": "conceptual_option", "options": ["suitable rated access platform", "qualified-person-designed scaffolding"],
            "constraints": ["floor/equipment loads, stability and anchorage", "aisle and work-platform footprint", "reach, fall protection and egress", "cleanability and labor time"],
            "safe_for_use_verified": False, "construction_instructions": None, "sources": ["scaffolds", "walking-surfaces"],
        },
        "water_options": {
            "municipal": {"role": "baseline option", "verified_available": False,
                          "requirements": ["supply reliability and quality", "treatment and sanitation", "tariff and wastewater charges"]},
            "rainwater": rainwater_potential(options.get("rainwater")),
            "groundwater": {"classification": "site_specific_option", "verified_available": False,
                            "cost": None, "requirements": ["aquifer/well yield and sustainable supply", "quality testing and treatment",
                                                           "local rights, drilling/pumping rules and permissions", "well, pumping, storage and treatment costs"],
                            "sources": ["groundwater"]},
            "condensate_reuse": {"classification": "experimental_option", "verified_usable": False,
                                 "requirements": ["collection yield, contamination testing, treatment and sanitation"]},
            "root_zone_method": "passive non-circulating nutrient solution; supply preparation does not authorize root-zone circulation",
        },
        "tenant_consolidation": _tenant_plan(inspection, options),
        "food_forest": build_food_forest_concept(options.get("food_forest")),
        "missing": sorted(set(missing)),
        "evidence_requests": ["confirm room geometry and equipment clearances", "obtain structural/load and electrical capacity evidence",
                              "confirm water quality/supply and crop requirements", "quantify HVAC, dehumidification and safe worker access"],
        "financial_handoff": {"agent": "michael", "basis": "per-scenario physical quantities and classified inputs",
                              "prices_or_financial_results_computed_here": False,
                              "instructions": "Price each alternative separately; unknown quantities/costs are not zero and no savings are assumed."},
        "sources": copy.deepcopy(list(SOURCES)),
        "limitations": ["No structural, electrical, zoning, food-safety or construction approval.",
                        "All capacity checks require supporting site evidence and appropriate review.",
                        "Reference inclusion is not model training or a claim of expert certification."],
    }
    from app.visual_design import concepts, canva_brief
    report['conceptualizations'] = concepts(report)
    report['visual_design_brief'] = canva_brief('onesimus', dossier_digest=report.get('source_dossier_digest'),
                                             package=report['conceptualizations'])
    return _seal(report)
