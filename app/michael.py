"""Michael: deterministic financial feasibility (canonical role).

Michael is the single financial authority. He consumes Onesimus's
engineering scenarios plus classified financial inputs and produces
inspectable money math in pure Python: capex, opex, revenue, operating
result, sensitivities, and break-even/payback only where defined.

Every consequential input identifies value, unit, currency, period,
source, observation date, classification, scenario, and uncertainty.
Unknown costs never silently become zero: assessments with missing
critical inputs return an insufficient-evidence verdict naming what is
needed. AI may explain results or propose assumptions; it cannot invent
computed outputs or approve unsupported conclusions.

Money uses Decimal with explicit HALF_UP rounding to cents; computed
figures serialize as strings. Projected revenue is not actual revenue,
and a completed calculation is not automatically favorable.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

MICHAEL_CALC_VERSION = "michael-4"

CLASSIFICATIONS = ("verified", "estimated", "experimental", "assumed", "unknown")

VERDICT_FAVORABLE = "favorable_under_assumptions"
VERDICT_UNFAVORABLE = "unfavorable_under_assumptions"
VERDICT_INSUFFICIENT = "insufficient_evidence"

# Labor/HVAC area allowances are legacy planning fallbacks. When Onesimus
# supplies physical hours/kWh, those quantities are priced instead.
FINANCIAL_NAMES = (
    "price_per_kg",
    "nutrient_usd_per_l",
    "electricity_usd_per_kwh",
    "water_usd_per_l",
    "labor_h_per_m2_week",
    "labor_usd_per_h",
    "hvac_usd_per_m2_year",
    "other_opex_usd_per_m2_year",
)

# Engineering outputs Michael consumes (from Onesimus scenarios).
ENGINEERING_NAMES = (
    "sites",
    "growing_m2",
    "kg_per_year",
    "water_l_per_year",
    "lighting_kwh_per_year",
)

# Configuration construction rates (planning assumptions owned here).
# Kept separate from Onesimus's physical configuration definitions.
COST_RATES: dict[str, dict[str, float]] = {
    "bench": {
        "rack_capex_usd_per_m2_tier": 40.0,
        "lighting_capex_usd_per_m2_tier": 60.0,
        "reservoir_capex_usd_per_site": 8.0,
    },
    "rack3": {
        "rack_capex_usd_per_m2_tier": 120.0,
        "lighting_capex_usd_per_m2_tier": 60.0,
        "reservoir_capex_usd_per_site": 3.0,
    },
}

# Model-proposed financial tunables: same names/ranges as the former
# shared catalog, now owned by the financial authority.
FINANCIAL_CATALOG: dict[str, dict[str, Any]] = {
    "price_per_kg": {"min": 0.5, "max": 50.0},
    "electricity_usd_per_kwh": {"min": 0.02, "max": 1.0},
}

_CENT = Decimal("0.01")


class MichaelError(ValueError):
    """Invalid financial request (unknown config, bad money, bad override)."""


class FinancialInput(BaseModel):
    """One consequential financial input with full provenance."""

    name: str
    value: Decimal | None = None
    unit: str = ""
    currency: str = "USD"
    period: str = ""
    source: str = ""
    observed_date: str = ""
    classification: str = "unknown"
    scenario: str = ""
    uncertainty: str = ""


FINANCIAL_UNITS={"price_per_kg":"USD/kg","nutrient_usd_per_l":"USD/L",
    "electricity_usd_per_kwh":"USD/kWh","water_usd_per_l":"USD/L",
    "labor_h_per_m2_week":"h/m2/week","labor_usd_per_h":"USD/h",
    "hvac_usd_per_m2_year":"USD/m2/year","other_opex_usd_per_m2_year":"USD/m2/year"}
FINANCIAL_PERIODS={"price_per_kg":{"per-kg","per-kg-saleable"},
    "nutrient_usd_per_l":{"per-liter","per-liter-solution","per-liter-concentrate"},
    "electricity_usd_per_kwh":{"per-kwh"},"water_usd_per_l":{"per-liter","per-liter-water"},
    "labor_h_per_m2_week":{"per-week","weekly","per-m2-week"},"labor_usd_per_h":{"per-hour"},
    "hvac_usd_per_m2_year":{"per-year","annual","per-m2-year"},
    "other_opex_usd_per_m2_year":{"per-year","annual","per-m2-year"}}


FINANCIAL_UNITS.update({"occupancy_usd_per_m2_year":"USD/m2/year", "maintenance_usd_per_year":"USD/year"})
FINANCIAL_PERIODS.update({name:{"per-year", "annual"} for name in ("occupancy_usd_per_m2_year", "maintenance_usd_per_year")})


def validate_financial_input(item):
    if item.name in ("occupancy_usd_per_m2_year", "maintenance_usd_per_year") and item.value is not None:
        if not item.value.is_finite() or item.value < 0:
            raise MichaelError(f"{item.name} must be finite and nonnegative")
    if item.classification not in CLASSIFICATIONS:
        raise MichaelError("unknown financial input classification")
    if item.currency != "USD":
        raise MichaelError("financial currency must be USD; no implicit exchange conversion")
    if item.name in FINANCIAL_UNITS and item.unit and item.unit != FINANCIAL_UNITS[item.name]:
        raise MichaelError(f"{item.name} requires {FINANCIAL_UNITS[item.name]}")
    if item.period and item.period.lower() not in FINANCIAL_PERIODS.get(item.name,set()):
        raise MichaelError(f"{item.name} has an incompatible period")
    return item


def money(value: Decimal | int | float | str) -> Decimal:
    """Quantize a monetary amount to cents (HALF_UP); reject non-finite."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MichaelError(f"not a monetary amount: {value!r}") from exc
    if not amount.is_finite():
        raise MichaelError(f"non-finite monetary amount: {value!r}")
    return amount.quantize(_CENT, rounding=ROUND_HALF_UP)


def money_str(value: Decimal | int | float | str) -> str:
    """Serialize a monetary amount for JSON-safe outputs."""
    return format(money(value), "f")


def default_market_inputs(scenario: str = "") -> dict[str, FinancialInput]:
    """Documented placeholder market inputs (assumed, not verified)."""
    return {
        "price_per_kg": FinancialInput(
            name="price_per_kg", value=Decimal("8.0"), unit="USD/kg",
            currency="USD", period="per-kg-saleable",
            source="mvp-market-assumption: placeholder wholesale price",
            observed_date="", classification="assumed",
            scenario=scenario, uncertainty="unbounded placeholder",
        ),
        "nutrient_usd_per_l": FinancialInput(
            name="nutrient_usd_per_l", value=Decimal("0.05"), unit="USD/L",
            currency="USD", period="per-liter-solution",
            source="mvp-market-assumption: cost per liter of prepared nutrient solution, not concentrate",
            observed_date="", classification="assumed",
            scenario=scenario, uncertainty="unbounded placeholder",
        ),
    }


def validate_financial_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """Validate model-proposed financial tunables against the catalog."""
    cleaned: dict[str, Any] = {}
    for name, value in overrides.items():
        rule = FINANCIAL_CATALOG.get(name)
        if rule is None:
            raise MichaelError(f"financial parameter not in catalog: {name}")
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise MichaelError(f"financial parameter {name} must be numeric")
        if not (float(rule["min"]) <= number <= float(rule["max"])):
            raise MichaelError(f"financial parameter {name} out of catalog range")
        cleaned[name] = number
    return cleaned


def _need(
    financial: dict[str, FinancialInput], name: str, missing: list[str]
) -> Decimal | None:
    candidate = financial.get(name)
    if candidate is None or candidate.value is None or candidate.classification == "unknown":
        missing.append(name)
        return None
    try:
        amount = Decimal(str(candidate.value))
    except (InvalidOperation, ValueError):
        missing.append(name)
        return None
    if not amount.is_finite() or amount < 0:
        missing.append(name)
        return None
    return amount


def _quantity(engineering: dict[str, Any], name: str) -> Decimal:
    try:
        value = Decimal(str(engineering[name]))
    except KeyError as exc:
        raise MichaelError(f"engineering output {name} is missing") from exc
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MichaelError(f"engineering output {name} is not numeric") from exc
    if not value.is_finite() or value < 0:
        raise MichaelError(f"engineering output {name} must be finite and nonnegative")
    return value


def assess(
    config_name: str,
    engineering: dict[str, Any],
    financial: dict[str, FinancialInput],
) -> dict[str, Any]:
    """Deterministic financial assessment over engineering + money inputs.

    Pure: no I/O, no AI calls. Returns complete=False with named gaps
    whenever engineering is incomplete or critical money inputs are
    missing; unknown costs are never treated as zero.
    """
    rates = COST_RATES.get(config_name)
    if rates is None:
        raise MichaelError(f"unknown scenario config: {config_name}")
    for item in financial.values():
        validate_financial_input(item)
    inputs_dump = {
        name: item.model_dump(mode="json") for name, item in financial.items()
    }
    base = {
        "michael_version": MICHAEL_CALC_VERSION,
        "config": config_name,
        "inputs": inputs_dump,
        "assumptions": [
            "Construction rates are planning assumptions owned by Michael "
            f"({config_name}: rack {rates['rack_capex_usd_per_m2_tier']}, "
            f"lighting {rates['lighting_capex_usd_per_m2_tier']}, "
            f"reservoir {rates['reservoir_capex_usd_per_site']} USD units).",
            "No financing, debt service, taxes, or incentives modeled; "
            "operating net equals revenue minus opex.",
            "Blank legacy units/periods use the named parameter's canonical units; no currency conversion is assumed.",
            "Projected figures are not actuals; verdicts hold only under "
            "the stated assumptions.",
        ],
    }
    if not engineering.get("complete"):
        missing_eng = list(engineering.get("missing", []) or ["engineering"])
        return {
            **base,
            "complete": False,
            "missing": [f"engineering:{name}" for name in missing_eng],
            "verdict": VERDICT_INSUFFICIENT,
            "verdict_reason": "engineering scenario incomplete; finance cannot assess",
            "evidence_requests": [
                f"complete engineering input: {name}" for name in missing_eng
            ],
            "outputs": {},
        }
    missing: list[str] = []
    outputs = engineering.get("outputs", {}) or {}
    if not isinstance(outputs, dict):
        raise MichaelError("engineering outputs must be an object")
    missing.extend(f"engineering:{name}" for name in ENGINEERING_NAMES
                   if name not in outputs or outputs[name] is None)
    physical_labor = (None if outputs.get("labor_hours_per_year") is None
                      else _quantity(outputs, "labor_hours_per_year"))
    physical_hvac = (None if outputs.get("hvac_kwh_per_year") is None
                     else _quantity(outputs, "hvac_kwh_per_year"))
    price = _need(financial, "price_per_kg", missing)
    nutrient = _need(financial, "nutrient_usd_per_l", missing)
    elec = _need(financial, "electricity_usd_per_kwh", missing)
    water_usd = _need(financial, "water_usd_per_l", missing)
    labor_h = (_need(financial, "labor_h_per_m2_week", missing)
               if physical_labor is None else None)
    labor_usd = _need(financial, "labor_usd_per_h", missing)
    hvac = (_need(financial, "hvac_usd_per_m2_year", missing)
            if physical_hvac is None else None)
    other = _need(financial, "other_opex_usd_per_m2_year", missing)
    nutrient_input = financial.get("nutrient_usd_per_l")
    nutrient_period = str(nutrient_input.period if nutrient_input else "").strip().lower()
    legacy_nutrient = nutrient_period in ("", "per-liter")
    if nutrient_period == "per-liter-concentrate":
        nutrient_basis = "concentrate"
        if outputs.get("nutrient_l_per_year") is None:
            missing.append("engineering:nutrient_l_per_year")
        nutrient_quantity = (None if outputs.get("nutrient_l_per_year") is None
                             else _quantity(outputs, "nutrient_l_per_year"))
    elif nutrient_period == "per-liter-solution" or legacy_nutrient:
        nutrient_basis = "prepared_solution"
        solution_name = ("nutrient_solution_l_per_year" if outputs.get("nutrient_solution_l_per_year") is not None
                         else "water_l_per_year")
        nutrient_quantity = (None if outputs.get(solution_name) is None
                             else _quantity(outputs, solution_name))
        if solution_name == "water_l_per_year":
            base["assumptions"].append(
                "Legacy prepared-solution volume uses water_l_per_year; separate cleaning-water/mixing quantities are unavailable.")
        if legacy_nutrient:
            base["assumptions"].append(
                "Legacy blank/per-liter nutrient price is assumed USD per liter of prepared solution, never concentrate.")
    else:
        nutrient_basis = "unknown"
        nutrient_quantity = None
        missing.append("nutrient_price_basis")
    if missing:
        unique = sorted(set(missing))
        return {
            **base,
            "complete": False,
            "missing": unique,
            "verdict": VERDICT_INSUFFICIENT,
            "verdict_reason": "critical financial inputs missing; no totals computed",
            "evidence_requests": [f"provide {name} with source and date" for name in unique],
            "outputs": {},
        }
    assert price is not None and nutrient is not None and elec is not None
    assert water_usd is not None and labor_usd is not None
    assert other is not None and nutrient_quantity is not None
    sites = _quantity(outputs, "sites")
    growing = _quantity(outputs, "growing_m2")
    kg = _quantity(outputs, "kg_per_year")
    liters = _quantity(outputs, "water_l_per_year")
    kwh = _quantity(outputs, "lighting_kwh_per_year")

    revenue = kg * price
    electricity_usd = kwh * elec
    water_cost = liters * water_usd
    nutrient_cost = nutrient_quantity * nutrient
    if physical_labor is not None:
        labor_cost = physical_labor * labor_usd
        labor_basis = "Onesimus labor_hours_per_year * USD/hour"
    else:
        assert labor_h is not None
        labor_cost = labor_h * growing * Decimal(52) * labor_usd
        labor_basis = "legacy assumed hours/m2/week * growing_m2 * 52 * USD/hour"
        base["assumptions"].append("Labor uses an explicit per-area planning allowance because physical labor hours are unquantified.")
    if physical_hvac is not None:
        hvac_cost = physical_hvac * elec
        hvac_basis = "Onesimus hvac_kwh_per_year * USD/kWh"
    else:
        assert hvac is not None
        hvac_cost = hvac * growing
        hvac_basis = "legacy assumed HVAC USD/m2/year * growing_m2"
        base["assumptions"].append("HVAC uses an explicit per-area cost allowance because physical HVAC energy is unquantified.")
    other_cost = other * growing
    occupancy_cost = Decimal(0)
    occupancy_quantified = False
    occupancy_input = financial.get("occupancy_usd_per_m2_year")
    if occupancy_input is not None and occupancy_input.value is not None and occupancy_input.classification != "unknown":
        try:
            occupancy_rate = Decimal(str(occupancy_input.value))
        except (InvalidOperation, ValueError):
            occupancy_rate = None
        if occupancy_rate is not None and occupancy_rate.is_finite():
            occupancy_cost = occupancy_rate * (_quantity(outputs, "site_floor_area_m2") if outputs.get("site_floor_area_m2") is not None else growing)
            occupancy_quantified = True
    maintenance_missing = []
    maintenance = _need(financial, "maintenance_usd_per_year", maintenance_missing) if "maintenance_usd_per_year" in financial else None
    maintenance_cost = maintenance if maintenance is not None else Decimal(0)
    opex = electricity_usd + water_cost + nutrient_cost + labor_cost + hvac_cost + other_cost + occupancy_cost + maintenance_cost
    capex = (
        growing * Decimal(str(rates["rack_capex_usd_per_m2_tier"]))
        + growing * Decimal(str(rates["lighting_capex_usd_per_m2_tier"]))
        + sites * Decimal(str(rates["reservoir_capex_usd_per_site"]))
    )
    net = revenue - opex
    payback = (capex / net) if net > 0 else None
    breakdown = {
        "electricity": money(electricity_usd),
        "water": money(water_cost),
        "nutrients": money(nutrient_cost),
        "labor": money(labor_cost),
        "hvac": money(hvac_cost),
        "other": money(other_cost),
    }
    if occupancy_quantified:
        breakdown["occupancy"] = money(occupancy_cost)
    if maintenance is not None:
        breakdown["maintenance"] = money(maintenance_cost)
    ranked = sorted(breakdown.items(), key=lambda item: item[1], reverse=True)
    drivers = [
        f"{name} cost {format(amount, 'f')} USD/yr" for name, amount in ranked[:3]
    ]
    drivers.append(
        f"revenue {format(money(revenue), 'f')} vs opex {format(money(opex), 'f')} USD/yr"
    )
    evidence_requests = [
        f"confirm {name} ({item.classification}: {item.source or 'no source'})"
        for name, item in sorted(financial.items())
        if item.classification != "verified"
    ]
    if not occupancy_quantified:
        evidence_requests.append(
            "occupancy cost not provided; excluded from opex "
            "(flagged, never assumed zero)"
        )
    if maintenance is None:
        evidence_requests.append("maintenance cost not provided; excluded from legacy totals; planning cases require it explicitly")
    base['assumptions'].append('Separately supplied maintenance is additional to other opex; the other-cost allowance must exclude maintenance to avoid double counting.')
    verdict = VERDICT_FAVORABLE if net > 0 else VERDICT_UNFAVORABLE
    return {
        **base,
        "complete": True,
        "missing": [],
        "verdict": verdict,
        "verdict_reason": (
            "operating net positive under stated assumptions"
            if net > 0
            else "operating net not positive under stated assumptions"
        ),
        "drivers": drivers,
        "evidence_requests": evidence_requests,
        "outputs": {
            "capex_usd": money_str(capex),
            "monthly_operating_cashflow_usd": money_str(net / Decimal(12)),
            "monthly_cashflow_basis": "Uniform monthly accrual/cash timing planning assumption; no seasonality, working capital, financing or tax model.",
            "maintenance_quantified": maintenance is not None,
            "opex_usd_per_year": money_str(opex),
            "opex_breakdown_usd": {name: format(amount, "f") for name, amount in breakdown.items()},
            "revenue_usd_per_year": money_str(revenue),
            "operating_net_usd_per_year": money_str(net),
            "simple_payback_years": float(payback.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if payback is not None else None,
            "occupancy_quantified": occupancy_quantified,
            "cost_basis": {
                "nutrients": {"basis": nutrient_basis, "liters_per_year": str(nutrient_quantity),
                              "price_period": "per-liter-solution" if legacy_nutrient else nutrient_period,
                              "legacy_assumption": legacy_nutrient},
                "labor": labor_basis, "hvac": hvac_basis,
                "occupancy": "supplied USD/m2/year times site_floor_area_m2 when available; legacy growing-area fallback otherwise",
                "maintenance": "explicit annual allowance; excluded from other opex",
            },
            "breakeven_kg_per_year": (
                float((opex / price).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
                if price > 0 else None
            ),
            "breakeven_price_usd_per_kg": (
                money_str(opex / kg) if kg > 0 else None
            ),
            "breakeven_note": (
                "Operating break-even: annual revenue must cover total "
                "opex at the stated price/yield (no fixed/variable "
                "cost split modeled)."
            ),
            "sensitivity": {
                "yield_plus_20pct_net_usd": money_str(revenue * Decimal("1.2") - opex),
                "yield_minus_20pct_net_usd": money_str(revenue * Decimal("0.8") - opex),
                "price_plus_20pct_net_usd": money_str(revenue * Decimal("1.2") - opex),
                "price_minus_20pct_net_usd": money_str(revenue * Decimal("0.8") - opex),
                "energy_cost_plus_20pct_net_usd": money_str(
                    revenue - (opex + (electricity_usd + (hvac_cost if physical_hvac is not None else 0)) * Decimal("0.2"))),
                "energy_cost_minus_20pct_net_usd": money_str(
                    revenue - (opex - (electricity_usd + (hvac_cost if physical_hvac is not None else 0)) * Decimal("0.2"))),
            },
        },
    }


def assess_scenarios(
    config_name: str,
    engineering: dict[str, Any],
    bundles: dict[str, dict[str, FinancialInput]],
) -> dict[str, Any]:
    """Run the same engineering through named assumption bundles.

    Conservative/base/optimistic are caller-supplied assumption
    sets, not forecasts: every verdict stays conditional on its
    bundle, and all arithmetic remains in :func:`assess`.
    """
    return {
        "scenarios": {
            name: assess(config_name, engineering, bundle)
            for name, bundle in bundles.items()
        },
        "note": (
            "Scenario labels describe assumption bundles only; no "
            "scenario is a verified outcome."
        ),
    }


def financial_inputs_from_raw(
    raw: dict[str, Any], *, scenario: str = "", provenance: str = "", classification: str = "unknown"
) -> dict[str, FinancialInput]:
    """Build a financial bundle from caller-supplied raw entries.

    Missing names become explicit unknown inputs (never silent zeros).
    """
    bundle: dict[str, FinancialInput] = {}
    for name in (*FINANCIAL_NAMES, *(name for name in ("occupancy_usd_per_m2_year", "maintenance_usd_per_year") if name in (raw or {}))):
        entry = (raw or {}).get(name)
        if isinstance(entry, dict) and entry.get("value") is not None:
            try:
                value: Decimal | None = Decimal(str(entry["value"]))
                if not value.is_finite():
                    value = None
            except (InvalidOperation, ValueError, TypeError):
                value = None
            bundle[name] = FinancialInput(
                name=name,
                value=value,
                unit=str(entry.get("unit", "")),
                currency=str(entry.get("currency", "USD")),
                period=str(entry.get("period", "")),
                source=str(entry.get("source", provenance)),
                observed_date=str(entry.get("observed_date", "")),
                classification=str(entry.get("classification", classification if value is None else "estimated")),
                scenario=scenario,
                uncertainty=str(entry.get("uncertainty", "")),
            )
        else:
            bundle[name] = FinancialInput(
                name=name, value=None, scenario=scenario,
                source=provenance, classification="unknown",
            )
    for item in bundle.values():
        validate_financial_input(item)
    return bundle


def planning_cases(config_name, engineering, financial):
    """Explicit deterministic stress cases, not market forecasts or confidence bounds."""
    from copy import deepcopy
    for item in financial.values():
        validate_financial_input(item)
    missing = []
    for name in ("occupancy_usd_per_m2_year", "maintenance_usd_per_year"):
        _need(financial, name, missing)
    if missing:
        return {"complete":False, "missing":sorted(set(missing)), "cases":{},
                "basis":"Occupancy and maintenance must be supplied explicitly, including sourced zero assumptions."}
    result = {}
    for name, revenue_factor, cost_factor in (("conservative",Decimal('0.8'),Decimal('1.2')),
                                             ("base",Decimal(1),Decimal(1)),
                                             ("optimistic",Decimal('1.2'),Decimal('0.8'))):
        scenario = deepcopy(engineering)
        if scenario.get('outputs',{}).get('kg_per_year') is not None:
            scenario['outputs']['kg_per_year'] = str(Decimal(str(scenario['outputs']['kg_per_year'])) * revenue_factor)
        bundle = {key:value.model_copy(deep=True) for key,value in financial.items()}
        for key, item in bundle.items():
            factor = revenue_factor if key == 'price_per_kg' else (cost_factor if key in (
                'nutrient_usd_per_l','electricity_usd_per_kwh','water_usd_per_l','labor_usd_per_h',
                'hvac_usd_per_m2_year','other_opex_usd_per_m2_year','maintenance_usd_per_year') else Decimal(1))
            if item.value is not None:
                item.value *= factor
                if factor != 1:
                    item.classification = 'assumed'
                    item.uncertainty = f'{name} planning stress multiplier {factor}; baseline source: '+item.source
                item.scenario = name
        assessment = assess(config_name, scenario, bundle)
        assessment['stress_assumptions'] = {'saleable_yield_multiplier':str(revenue_factor),
            'sale_price_multiplier':str(revenue_factor),'variable_rate_multiplier':str(cost_factor),
            'occupancy_and_capex':'held constant; provided planning basis',
            'scope':'Independent planning cases; not probabilities, predictions or guaranteed returns.'}
        result[name] = assessment
    return {'complete':all(case['complete'] for case in result.values()), 'cases':result,
            'missing':sorted({gap for case in result.values() for gap in case.get('missing',[])})}
