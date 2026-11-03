"""Michael deterministic finance tests (pure calculations, no PG)."""

from __future__ import annotations
from app.michael import FINANCIAL_UNITS

from decimal import Decimal
from typing import Any

import pytest

from app.agents.onesimus.crops import crop_profile
from app.agents.onesimus.service import ClassifiedInput, evaluate_scenario
from app.michael import (
    FINANCIAL_CATALOG,
    FINANCIAL_NAMES,
    MICHAEL_CALC_VERSION,
    FinancialInput,
    MichaelError,
    assess,
    default_market_inputs,
    financial_inputs_from_raw,
    money_str,
    validate_financial_overrides,
)


def _engineering(config: str = "bench") -> dict[str, Any]:
    site = {
        "usable_area_m2": ClassifiedInput(
            name="usable_area_m2", value=100.0, unit="m2",
            provenance="test", classification="assumed",
        )
    }
    result = evaluate_scenario(config, crop_profile(), site)
    assert result["complete"] is True
    return {
        "complete": result["complete"],
        "missing": result["missing"],
        "outputs": result["outputs"],
    }


def _finance(**overrides: Any) -> dict[str, FinancialInput]:
    values = {
        "price_per_kg": "8.0",
        "nutrient_usd_per_l": "0.05",
        "electricity_usd_per_kwh": "0.12",
        "water_usd_per_l": "0.002",
        "labor_h_per_m2_week": "0.5",
        "labor_usd_per_h": "18.0",
        "hvac_usd_per_m2_year": "8.0",
        "other_opex_usd_per_m2_year": "5.0",
    }
    values.update(overrides)
    return {
        name: FinancialInput(
            name=name, value=Decimal(text), unit=FINANCIAL_UNITS[name], currency="USD",
            period="per-liter-solution" if name == "nutrient_usd_per_l" else "", source="test", observed_date="2026-10-30",
            classification="estimated", scenario="bench", uncertainty="",
        )
        for name, text in values.items()
    }


def test_assess_bench_hand_check() -> None:
    # Fresh solution each of ten crop cycles: 112500 L/year. Revenue
    # 30000; opex 1839.60+225+5625+46800+800+500 =55789.60.
    result = assess("bench", _engineering("bench"), _finance())
    assert result["complete"] is True
    assert result["michael_version"] == MICHAEL_CALC_VERSION
    assert result["verdict"] == "unfavorable_under_assumptions"
    out = result["outputs"]
    assert out["capex_usd"] == "30000.00"
    assert out["revenue_usd_per_year"] == "30000.00"
    assert out["opex_usd_per_year"] == "55789.60"
    assert out["operating_net_usd_per_year"] == "-25789.60"
    assert out["opex_breakdown_usd"]["labor"] == "46800.00"
    assert out["simple_payback_years"] is None
    assert out["breakeven_kg_per_year"] == pytest.approx(6973.7, abs=0.1)
    assert out["breakeven_price_usd_per_kg"] == "14.88"
    assert "break-even" in out["breakeven_note"].lower()
    assert out["occupancy_quantified"] is False
    assert out["sensitivity"]["yield_plus_20pct_net_usd"] == "-19789.60"


def test_assess_profitable_payback_defined() -> None:
    result = assess(
        "bench", _engineering("bench"),
        _finance(labor_h_per_m2_week="0.1", labor_usd_per_h="15.0"),
    )
    out = result["outputs"]
    assert out["operating_net_usd_per_year"] == "13210.40"
    assert result["verdict"] == "favorable_under_assumptions"
    assert out["simple_payback_years"] == pytest.approx(30000.0 / 13210.4, abs=0.01)


def test_assess_rack3_uses_own_rates() -> None:
    result = assess("rack3", _engineering("rack3"), _finance())
    assert result["outputs"]["capex_usd"] == "76500.00"
    assert result["complete"] is True


def test_missing_financial_input_is_insufficient() -> None:
    bundle = _finance()
    del bundle["labor_usd_per_h"]
    result = assess("bench", _engineering("bench"), bundle)
    assert result["complete"] is False
    assert result["verdict"] == "insufficient_evidence"
    assert result["missing"] == ["labor_usd_per_h"]
    assert result["outputs"] == {}
    assert any("labor_usd_per_h" in request for request in result["evidence_requests"])


def test_unknown_cost_never_becomes_zero() -> None:
    bundle = _finance()
    bundle["hvac_usd_per_m2_year"] = FinancialInput(
        name="hvac_usd_per_m2_year", value=None, classification="unknown",
        scenario="bench",
    )
    result = assess("bench", _engineering("bench"), bundle)
    assert result["complete"] is False
    assert "hvac_usd_per_m2_year" in result["missing"]


def test_incomplete_engineering_blocks_finance() -> None:
    result = assess(
        "bench",
        {"complete": False, "missing": ["usable_area_m2"], "outputs": {}},
        _finance(),
    )
    assert result["complete"] is False
    assert result["verdict"] == "insufficient_evidence"
    assert result["missing"] == ["engineering:usable_area_m2"]


def test_unknown_config_rejected() -> None:
    with pytest.raises(MichaelError, match="unknown scenario config"):
        assess("aeroponic", _engineering("bench"), _finance())


def test_money_precision_and_nonfinite() -> None:
    assert money_str(Decimal("0.1") + Decimal("0.2")) == "0.30"
    with pytest.raises(MichaelError, match="non-finite"):
        money_str(float("nan"))
    with pytest.raises(MichaelError, match="non-finite"):
        money_str(float("inf"))


def test_drivers_rank_costs_and_flag_unverified() -> None:
    result = assess("bench", _engineering("bench"), _finance())
    assert result["drivers"][0].startswith("labor cost")
    assert any("price_per_kg" in request and "estimated" in request
               for request in result["evidence_requests"])
    assert any("financing" in note for note in result["assumptions"])


def test_validate_financial_overrides_catalog() -> None:
    assert validate_financial_overrides({"price_per_kg": 9.5}) == {"price_per_kg": 9.5}
    with pytest.raises(MichaelError, match="not in catalog"):
        validate_financial_overrides({"yield_per_head": 1.0})
    with pytest.raises(MichaelError, match="out of catalog range"):
        validate_financial_overrides({"price_per_kg": 500.0})
    assert set(FINANCIAL_CATALOG) == {"price_per_kg", "electricity_usd_per_kwh"}


def test_financial_inputs_from_raw_marks_unknown() -> None:
    bundle = financial_inputs_from_raw(
        {"price_per_kg": {"value": 8.0, "unit": "USD/kg"}}, scenario="bench"
    )
    assert set(bundle) == set(FINANCIAL_NAMES)
    assert bundle["price_per_kg"].value == Decimal("8.0")
    assert bundle["labor_usd_per_h"].value is None
    assert bundle["labor_usd_per_h"].classification == "unknown"
    malformed = financial_inputs_from_raw({"price_per_kg": {"value": "nonsense"}})
    assert malformed["price_per_kg"].value is None


def test_finance_identical_in_both_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    from pathlib import Path

    monkeypatch.setenv("DEMO_MODE", "yes")
    before = assess("bench", _engineering("bench"), _finance())
    monkeypatch.setenv("DEMO_MODE", "no")
    after = assess("bench", _engineering("bench"), _finance())
    assert json.dumps(before, sort_keys=True, default=str) == json.dumps(
        after, sort_keys=True, default=str
    )
    text = (Path(__file__).resolve().parent.parent / "app" / "michael.py").read_text()
    assert "DEMO_MODE" not in text
    assert "app.modes" not in text


def test_default_market_inputs_are_assumed() -> None:
    defaults = default_market_inputs("bench")
    assert set(defaults) == {"price_per_kg", "nutrient_usd_per_l"}
    for item in defaults.values():
        assert item.classification == "assumed"
        assert item.scenario == "bench"
    assert defaults["nutrient_usd_per_l"].period == "per-liter-solution"
    assert "not concentrate" in defaults["nutrient_usd_per_l"].source


def _quantified_engineering():
    return {"complete": True, "missing": [], "outputs": {
        "sites": 100, "growing_m2": 10, "kg_per_year": 100,
        "water_l_per_year": 1000, "nutrient_solution_l_per_year": 900,
        "lighting_kwh_per_year": 100, "nutrient_l_per_year": 10,
        "labor_hours_per_year": 20, "hvac_kwh_per_year": 200}}


def test_physical_quantities_price_concentrate_labor_and_hvac_without_area_double_count():
    bundle = _finance(nutrient_usd_per_l="5", labor_usd_per_h="3",
                      electricity_usd_per_kwh="0.1", other_opex_usd_per_m2_year="1")
    bundle["nutrient_usd_per_l"].period = "per-liter-concentrate"
    # No area allowance is needed or charged when physical quantities exist.
    del bundle["labor_h_per_m2_week"]
    del bundle["hvac_usd_per_m2_year"]
    result = assess("bench", _quantified_engineering(), bundle)
    assert result["complete"] is True
    costs = result["outputs"]["opex_breakdown_usd"]
    assert costs == {"electricity": "10.00", "water": "2.00", "nutrients": "50.00",
                     "labor": "60.00", "hvac": "20.00", "other": "10.00"}
    assert result["outputs"]["opex_usd_per_year"] == "152.00"
    assert result["outputs"]["operating_net_usd_per_year"] == "648.00"
    assert result["outputs"]["cost_basis"]["nutrients"]["liters_per_year"] == "10"
    # Electricity sensitivity includes physical HVAC energy as well as lights.
    assert result["outputs"]["sensitivity"]["energy_cost_plus_20pct_net_usd"] == "642.00"


def test_concentrate_price_requires_concentrate_quantity_never_substitutes_water():
    engineering = _quantified_engineering()
    del engineering["outputs"]["nutrient_l_per_year"]
    bundle = _finance()
    bundle["nutrient_usd_per_l"].period = "per-liter-concentrate"
    result = assess("bench", engineering, bundle)
    assert result["complete"] is False
    assert result["missing"] == ["engineering:nutrient_l_per_year"]
    assert result["outputs"] == {}


@pytest.mark.parametrize("period", ["", "per-liter", "per-liter-solution"])
def test_prepared_solution_pricing_uses_mixed_solution_not_cleaning_water(period):
    bundle = _finance()
    bundle["nutrient_usd_per_l"].period = period
    result = assess("bench", _quantified_engineering(), bundle)
    assert result["outputs"]["opex_breakdown_usd"]["nutrients"] == "45.00"
    basis = result["outputs"]["cost_basis"]["nutrients"]
    assert basis["basis"] == "prepared_solution"
    assert basis["legacy_assumption"] is (period != "per-liter-solution")


def test_unsupported_nutrient_price_basis_is_refused():
    bundle = _finance()
    bundle["nutrient_usd_per_l"].period = "per-gallon"
    with pytest.raises(MichaelError, match="incompatible period"):
        assess("bench", _quantified_engineering(), bundle)


def test_missing_required_engineering_output_does_not_become_zero():
    engineering = _quantified_engineering()
    del engineering["outputs"]["kg_per_year"]
    result = assess("bench", engineering, _finance())
    assert result["complete"] is False
    assert "engineering:kg_per_year" in result["missing"]


@pytest.mark.parametrize("name,value", [("labor_hours_per_year", -1),
                                        ("hvac_kwh_per_year", float("nan"))])
def test_invalid_optional_physical_quantity_is_refused(name, value):
    engineering = _quantified_engineering()
    engineering["outputs"][name] = value
    with pytest.raises(MichaelError):
        assess("bench", engineering, _finance())


def test_occupancy_included_only_when_provided() -> None:
    from app.michael import FinancialInput

    bundle = _finance()
    bundle["occupancy_usd_per_m2_year"] = FinancialInput(
        name="occupancy_usd_per_m2_year", value=Decimal("10"),
        classification="estimated", scenario="bench")
    result = assess("bench", _engineering("bench"), bundle)
    out = result["outputs"]
    assert out["occupancy_quantified"] is True
    assert out["opex_breakdown_usd"]["occupancy"] == "1000.00"
    assert out["opex_usd_per_year"] == "56789.60"


def test_energy_sensitivity_and_scenarios() -> None:
    from app.michael import assess_scenarios

    result = assess("bench", _engineering("bench"), _finance())
    sensitivity = result["outputs"]["sensitivity"]
    # Electricity is 1839.60 USD/yr in the hand-check bundle.
    assert sensitivity["energy_cost_plus_20pct_net_usd"] == "-26157.52"
    assert sensitivity["energy_cost_minus_20pct_net_usd"] == "-25421.68"
    bundles = {
        "conservative": _finance(price_per_kg="6.0"),
        "base": _finance(),
        "optimistic": _finance(price_per_kg="10.0"),
    }
    scenarios = assess_scenarios("bench", _engineering("bench"), bundles)
    nets = {
        name: Decimal(item["outputs"]["operating_net_usd_per_year"])
        for name, item in scenarios["scenarios"].items()
    }
    assert nets["conservative"] < nets["base"] < nets["optimistic"]


@pytest.mark.parametrize("field,value", [("currency", "EUR"), ("unit", "USD/gallon"),
                                         ("classification", "invented"), ("period", "daily")])
def test_financial_unit_currency_and_classification_refuse(field, value):
    bundle = _finance()
    setattr(bundle["water_usd_per_l"], field, value)
    with pytest.raises(MichaelError):
        assess("bench", _quantified_engineering(), bundle)


def test_raw_financial_input_does_not_invent_observation_date():
    from app.michael import financial_inputs_from_raw
    raw = {"water_usd_per_l": {"value": "0.001", "unit": "USD/L", "classification": "assumed"}}
    assert financial_inputs_from_raw(raw)["water_usd_per_l"].observed_date == ""
