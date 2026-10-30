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

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

MICHAEL_CALC_VERSION = "michael-1"

CLASSIFICATIONS = ("verified", "estimated", "experimental", "assumed", "unknown")

VERDICT_FAVORABLE = "favorable_under_assumptions"
VERDICT_UNFAVORABLE = "unfavorable_under_assumptions"
VERDICT_INSUFFICIENT = "insufficient_evidence"

# Financial inputs Michael requires per assessment. Labor hours are an
# operations input consumed here because only the labor-cost math needs
# them; Onesimus engineering does not.
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
            currency="USD", period="per-liter",
            source="mvp-market-assumption: nutrient concentrate allowance",
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
    if candidate is None or candidate.value is None:
        missing.append(name)
        return None
    try:
        amount = Decimal(str(candidate.value))
    except (InvalidOperation, ValueError):
        missing.append(name)
        return None
    if not amount.is_finite():
        missing.append(name)
        return None
    return amount


def _quantity(engineering: dict[str, Any], name: str) -> Decimal:
    try:
        value = Decimal(str(engineering.get(name, 0)))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MichaelError(f"engineering output {name} is not numeric") from exc
    if not value.is_finite():
        raise MichaelError(f"engineering output {name} is non-finite")
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
    price = _need(financial, "price_per_kg", missing)
    nutrient = _need(financial, "nutrient_usd_per_l", missing)
    elec = _need(financial, "electricity_usd_per_kwh", missing)
    water_usd = _need(financial, "water_usd_per_l", missing)
    labor_h = _need(financial, "labor_h_per_m2_week", missing)
    labor_usd = _need(financial, "labor_usd_per_h", missing)
    hvac = _need(financial, "hvac_usd_per_m2_year", missing)
    other = _need(financial, "other_opex_usd_per_m2_year", missing)
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
    assert water_usd is not None and labor_h is not None and labor_usd is not None
    assert hvac is not None and other is not None
    outputs = engineering.get("outputs", {}) or {}
    sites = _quantity(outputs, "sites")
    growing = _quantity(outputs, "growing_m2")
    kg = _quantity(outputs, "kg_per_year")
    liters = _quantity(outputs, "water_l_per_year")
    kwh = _quantity(outputs, "lighting_kwh_per_year")

    revenue = kg * price
    electricity_usd = kwh * elec
    water_cost = liters * water_usd
    nutrient_cost = liters * nutrient
    labor_cost = labor_h * growing * Decimal(52) * labor_usd
    hvac_cost = hvac * growing
    other_cost = other * growing
    opex = electricity_usd + water_cost + nutrient_cost + labor_cost + hvac_cost + other_cost
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
            "opex_usd_per_year": money_str(opex),
            "opex_breakdown_usd": {name: format(amount, "f") for name, amount in breakdown.items()},
            "revenue_usd_per_year": money_str(revenue),
            "operating_net_usd_per_year": money_str(net),
            "simple_payback_years": float(payback.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if payback is not None else None,
            "breakeven_kg_per_year": None,
            "breakeven_note": "No fixed/variable cost split modeled; break-even output undefined.",
            "sensitivity": {
                "yield_plus_20pct_net_usd": money_str(revenue * Decimal("1.2") - opex),
                "yield_minus_20pct_net_usd": money_str(revenue * Decimal("0.8") - opex),
                "price_plus_20pct_net_usd": money_str(revenue * Decimal("1.2") - opex),
                "price_minus_20pct_net_usd": money_str(revenue * Decimal("0.8") - opex),
            },
        },
    }


def financial_inputs_from_raw(
    raw: dict[str, Any], *, scenario: str = "", provenance: str = "", classification: str = "unknown"
) -> dict[str, FinancialInput]:
    """Build a financial bundle from caller-supplied raw entries.

    Missing names become explicit unknown inputs (never silent zeros).
    """
    today = date.today().isoformat()
    bundle: dict[str, FinancialInput] = {}
    for name in FINANCIAL_NAMES:
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
                observed_date=str(entry.get("observed_date", today if value is not None else "")),
                classification=str(entry.get("classification", classification if value is None else "estimated")),
                scenario=scenario,
                uncertainty=str(entry.get("uncertainty", "")),
            )
        else:
            bundle[name] = FinancialInput(
                name=name, value=None, scenario=scenario,
                source=provenance, classification="unknown",
            )
    return bundle
