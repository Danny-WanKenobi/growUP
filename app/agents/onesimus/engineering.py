"""Pure physical calculations; all capacities remain unverified until evidenced."""
from __future__ import annotations

import math
from typing import Any
from app.workflow_db import WorkflowError

SITE_UNITS = {
    "usable_area_m2": "m2", "room_length_m": "m", "room_width_m": "m",
    "clear_height_m": "m", "rack_length_m": "m", "rack_width_m": "m",
    "aisle_width_m": "m", "service_clearance_m": "m", "tier_pitch_m": "m",
    "ceiling_clearance_m": "m", "hvac_w_per_m2": "W/m2",
    "cleaning_l_per_site_cycle": "L/site/cycle",
    "hvac_hours_per_day": "h/day", "fixed_labor_hours_per_year": "h/year",
}
CROP_UNITS = {
    "spacing_m": "m", "cycles_per_year": "cycles/year",
    "saleable_kg_per_head": "kg/head", "reservoir_l_per_site": "L/site",
    "topup_l_per_site_cycle": "L/site/cycle", "root_zone_air_fraction": "fraction",
    "light_w_per_m2": "W/m2", "light_hours_per_day": "h/day",
    "nutrient_ml_per_site_cycle": "mL/site/cycle",
    "nutrient_ml_per_l_solution": "mL/L", "labor_min_per_site_cycle": "min/site/cycle",
    "ppfd_umol_m2_s": "umol/m2/s", "fixture_efficacy_umol_j": "umol/J",
}
_CONVERSIONS = {
    ("ft2", "m2"): .09290304, ("sqft", "m2"): .09290304,
    ("ft", "m"): .3048, ("cm", "m"): .01, ("mm", "m"): .001,
    ("g/head", "kg/head"): .001, ("mL/site", "L/site"): .001,
    ("mL/site/cycle", "L/site/cycle"): .001,
    ("L/site/cycle", "mL/site/cycle"): 1000,
    ("h/site/cycle", "min/site/cycle"): 60,
}


def quantity(inputs: dict, name: str, units: dict, *, default=None):
    item = inputs.get(name)
    if item is None or item.value is None:
        return default
    if item.classification == "unknown":
        return default
    value = float(item.value)
    if not math.isfinite(value) or value < 0:
        raise WorkflowError(f"{name} must be finite and nonnegative")
    expected = units[name]
    unit = item.unit.replace("²", "2").replace("µ", "u").strip()
    if unit != expected:
        factor = _CONVERSIONS.get((unit, expected))
        if factor is None:
            raise WorkflowError(f"{name} requires {expected}, not {unit!r}")
        value *= factor
    # Computational range, not a claim of physical feasibility. Refuse extreme
    # exponents before squaring spacing or multiplying annual resource totals.
    if not math.isfinite(value) or value > 1e12 or (0 < value < 1e-9):
        raise WorkflowError(f"{name} outside supported numeric range after unit conversion")
    return value


def calculate(config, crop, site, *, tenant_consolidation, observed_occupancy,
              tenant_flags, calculation_version):
    inputs = {"crop": {k: v.model_dump() for k, v in crop.items()},
              "site": {k: v.model_dump() for k, v in site.items()}}
    # Validate even optional supplied values; NaN or incompatible units must
    # not disappear merely because another required input is missing.
    cv = {n: quantity(crop, n, CROP_UNITS) for n in CROP_UNITS}
    sv = {n: quantity(site, n, SITE_UNITS) for n in SITE_UNITS}
    for name in ("usable_area_m2", "room_length_m", "room_width_m", "clear_height_m",
                 "rack_length_m", "rack_width_m", "tier_pitch_m", "aisle_width_m"):
        if sv[name] is not None and sv[name] <= 0:
            raise WorkflowError(f"{name} must be positive")
    for name in ("spacing_m", "cycles_per_year", "saleable_kg_per_head", "reservoir_l_per_site",
                 "light_w_per_m2", "light_hours_per_day", "fixture_efficacy_umol_j"):
        if cv[name] is not None and cv[name] <= 0:
            raise WorkflowError(f"{name} must be positive")
    if cv["light_hours_per_day"] is not None and cv["light_hours_per_day"] > 24:
        raise WorkflowError("photoperiod cannot exceed 24 hours/day")
    if cv["cycles_per_year"] is not None and cv["cycles_per_year"] > 365:
        raise WorkflowError("cycles_per_year exceeds the supported daily-cycle bound")
    if sv["hvac_hours_per_day"] is not None and sv["hvac_hours_per_day"] > 24:
        raise WorkflowError("HVAC operating hours cannot exceed 24 hours/day")
    air = cv["root_zone_air_fraction"]
    if air is not None and not 0 < air < 1:
        raise WorkflowError("root_zone_air_fraction must be between zero and one")
    base = {
        "config": config.name, "inputs": {**inputs["crop"], **inputs["site"]},
        "input_groups": inputs, "calculation_version": calculation_version,
        "observed_occupancy": observed_occupancy,
        "tenant_flags": list(tenant_flags) if tenant_consolidation else [],
        "assumptions": [
            "Passive Kratky: static nutrient solution; preserve root-zone air space; no recirculation.",
            "Initial nutrient solution is replaced for each crop cycle; reuse is not assumed.",
            f"Tenant consolidation hypothetical: {tenant_consolidation}; actual occupancy unchanged.",
        ],
        "limitations": ["Conceptual estimates only; not a construction design or building-capacity approval.",
                        "Michael owns prices, costs, revenues and financial conclusions."],
        "capacity_status": "unverified", "engineering_complete": False,
        "formulas": {"sites": "integer rack grids * racks * tiers; area-only fallback floor(area/spacing^2)",
                     "yield_kg_year": "sites * cycles/year * saleable kg/head",
                     "water_l_year": "sites * cycles/year * (initial fill + topup + quantified cleaning)",
                     "lighting_kwh_year": "lit m2 * W/m2 * hours/day * 365 / 1000"},
    }
    required = ("spacing_m", "cycles_per_year", "saleable_kg_per_head", "reservoir_l_per_site",
                "topup_l_per_site_cycle", "root_zone_air_fraction", "light_w_per_m2", "light_hours_per_day")
    missing = [n for n in required if cv[n] is None]
    if sv["usable_area_m2"] is None:
        missing.append("usable_area_m2")
    if missing:
        return {**base, "complete": False, "calculation_complete": False,
                "layout_status": "missing_inputs", "missing": sorted(missing), "outputs": {}}
    area, spacing = sv["usable_area_m2"], cv["spacing_m"]
    dims = ("room_length_m", "room_width_m", "clear_height_m")
    absent_dims = [n for n in dims if sv[n] is None]
    layout = {"status": "area_only_estimate", "missing": absent_dims}
    floor_area = area
    sites_per_tier = math.floor(area / spacing**2 + 1e-9)
    if not absent_dims:
        length, width, height = (sv[n] for n in dims)
        rl, rw = sv["rack_length_m"] or 2., sv["rack_width_m"] or 1.
        aisle = sv["aisle_width_m"] if sv["aisle_width_m"] is not None else 1.
        margin = sv["service_clearance_m"] if sv["service_clearance_m"] is not None else .5
        pitch = sv["tier_pitch_m"] or .9
        ceiling = sv["ceiling_clearance_m"] if sv["ceiling_clearance_m"] is not None else .3
        base["assumptions"].append("Unspecified rack dimensions 2m x 1m, aisle 1m, perimeter 0.5m, tier pitch 0.9m and ceiling gap 0.3m are assumed catalog values, not safety standards.")
        cols = max(0, math.floor((length-2*margin+aisle)/(rl+aisle)))
        rows = max(0, math.floor((width-2*margin+aisle)/(rw+aisle)))
        racks = min(cols*rows, math.floor(area/(rl*rw)+1e-9))
        sites_per_rack = math.floor(rl/spacing+1e-9)*math.floor(rw/spacing+1e-9)
        blockers = []
        if area > length*width + 1e-9:
            blockers.append("usable area exceeds room floor area")
        if config.tiers*pitch+ceiling > height + 1e-9:
            blockers.append("tiers, crop/light pitch and ceiling gap exceed room height")
        if racks == 0 or sites_per_rack == 0:
            blockers.append("no whole rack/container grid fits with the stated access allowances")
        layout = {"status": "impossible" if blockers else "plausible_under_assumptions",
                  "racks": racks, "rack_length_m": rl, "rack_width_m": rw,
                  "aisle_width_m": aisle, "service_clearance_m": margin,
                  "required_height_m": config.tiers*pitch+ceiling, "blockers": blockers}
        if blockers:
            return {**base, "layout": layout, "layout_status": "impossible", "complete": False,
                    "calculation_complete": False, "missing": blockers, "outputs": {}}
        floor_area = racks*rl*rw
        sites_per_tier = racks*sites_per_rack
    sites = sites_per_tier*config.tiers
    if sites <= 0:
        return {**base, "complete": False, "calculation_complete": False,
                "layout_status": "impossible", "missing": ["no whole planting site fits"], "outputs": {}}
    cycles = cv["cycles_per_year"]
    growing = floor_area*config.tiers
    fill = sites*cv["reservoir_l_per_site"]
    water = sites*cycles*(cv["reservoir_l_per_site"]+cv["topup_l_per_site_cycle"])
    solution_water = water
    not_quantified = []
    if sv["cleaning_l_per_site_cycle"] is not None:
        water += sites*cycles*sv["cleaning_l_per_site_cycle"]
    else:
        not_quantified.append("cleaning_water")
    watts = cv["light_w_per_m2"]
    if cv["ppfd_umol_m2_s"] is not None and cv["fixture_efficacy_umol_j"] is not None:
        watts = cv["ppfd_umol_m2_s"]/cv["fixture_efficacy_umol_j"]
        base["assumptions"].append("PPFD/fixture efficacy estimates light power at the crop; optical losses remain unverified.")
    outputs = {
        "sites": sites, "containers": sites, "tiers": config.tiers,
        "site_floor_area_m2": round(area, 3),
        "growing_m2": round(growing, 3), "rack_footprint_m2": round(floor_area, 3),
        "heads_per_year": sites*cycles, "kg_per_year": round(sites*cycles*cv["saleable_kg_per_head"], 3),
        "reservoir_l_total": round(fill, 3), "water_l_per_year": round(water, 3),
        "nutrient_solution_l_per_year": round(solution_water, 3),
        "container_capacity_l_total": round(fill/(1-air), 3),
        "root_zone_air_l_total": round(fill*air/(1-air), 3),
        "lighting_kwh_per_year": round(growing*watts*cv["light_hours_per_day"]*365/1000, 3),
        "lighting_connected_kw": round(growing*watts/1000, 3),
        "water_plus_structure_kg": round(fill+growing*config.structure_kg_per_m2_tier, 3),
    }
    if cv["ppfd_umol_m2_s"] is not None:
        outputs["dli_mol_m2_day"] = cv["ppfd_umol_m2_s"]*cv["light_hours_per_day"]*3600/1e6
    else:
        not_quantified.append("crop_light_delivery")
    for name, value in (
        ("hvac_kwh_per_year", None if sv["hvac_w_per_m2"] is None else growing*sv["hvac_w_per_m2"]*(sv["hvac_hours_per_day"] if sv["hvac_hours_per_day"] is not None else 24)*365/1000),
        ("nutrient_l_per_year", None if cv["nutrient_ml_per_site_cycle"] is None else sites*cycles*cv["nutrient_ml_per_site_cycle"]/1000),
        ("labor_hours_per_year", None if cv["labor_min_per_site_cycle"] is None else sites*cycles*cv["labor_min_per_site_cycle"]/60+(sv["fixed_labor_hours_per_year"] or 0)),
    ):
        if value is None:
            not_quantified.append({"hvac_kwh_per_year": "hvac_energy", "nutrient_l_per_year": "nutrient_volume", "labor_hours_per_year": "labor_hours"}[name])
        else:
            outputs[name] = round(value, 3)
    if "nutrient_l_per_year" not in outputs and cv["nutrient_ml_per_l_solution"] is not None:
        outputs["nutrient_l_per_year"] = round(solution_water*cv["nutrient_ml_per_l_solution"]/1000, 3)
        not_quantified.remove("nutrient_volume")
    base["formulas"].update({"labor_hours_per_year":"sites * cycles/year * labor_min_per_site_cycle / 60 + supplied fixed annual hours",
        "hvac_kwh_per_year":"growing_m2 * HVAC W/m2 * operating hours/day * 365 / 1000",
        "nutrient_l_per_year":"solution liters/year * supplied mL concentrate/L / 1000, or supplied mL/site/cycle basis"})
    base["assumptions"].append("HVAC defaults to 24 operating hours/day only when hours are absent; this is an allowance, not a load calculation. Fixed labor is excluded unless supplied.")
    base["assumptions"].append("Container capacity reserves the specified air fraction; refill procedures must not submerge established air roots.")
    return {**base, "crop_allocation": [{"crop_id": "butterhead_lettuce_kratky_mvp1", "share": 1.0,
                                          "sites": sites, "cycles_per_year": cycles}],
            "complete": True, "calculation_complete": True,
            "layout": layout, "layout_status": layout["status"], "missing": [],
            "not_quantified": not_quantified, "outputs": outputs,
            "output_units": {"site_floor_area_m2":"m2", "sites": "count", "containers": "count", "growing_m2": "m2",
                             "water_l_per_year": "L/year", "nutrient_l_per_year": "L concentrate/year",
                             "nutrient_solution_l_per_year": "L solution/year",
                             "tiers": "count", "rack_footprint_m2": "m2", "heads_per_year": "heads/year",
                             "kg_per_year": "kg/year", "reservoir_l_total": "L",
                             "container_capacity_l_total": "L", "root_zone_air_l_total": "L",
                             "lighting_connected_kw": "kW", "dli_mol_m2_day": "mol/m2/day",
                             "lighting_kwh_per_year": "kWh/year", "hvac_kwh_per_year": "kWh/year",
                             "labor_hours_per_year": "h/year", "water_plus_structure_kg": "kg"}}
