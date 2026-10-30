"""Michael deterministic finance tests (pure calculations, no PG)."""

from __future__ import annotations

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
            name=name, value=Decimal(text), unit="u", currency="USD",
            period="p", source="test", observed_date="2026-10-30",
            classification="estimated", scenario="bench", uncertainty="",
        )
        for name, text in values.items()
    }


def test_assess_bench_hand_check() -> None:
    # Engineering: 2500 sites, 100 growing m2, 3750 kg/yr, 22500 L,
    # 15330 kWh. Revenue 30000; opex 1839.60+45.00+1125.00+46800.00
    # +800.00+500.00 = 51109.60; net -21109.60; capex 30000.00.
    result = assess("bench", _engineering("bench"), _finance())
    assert result["complete"] is True
    assert result["michael_version"] == MICHAEL_CALC_VERSION
    assert result["verdict"] == "unfavorable_under_assumptions"
    out = result["outputs"]
    assert out["capex_usd"] == "30000.00"
    assert out["revenue_usd_per_year"] == "30000.00"
    assert out["opex_usd_per_year"] == "51109.60"
    assert out["operating_net_usd_per_year"] == "-21109.60"
    assert out["opex_breakdown_usd"]["labor"] == "46800.00"
    assert out["simple_payback_years"] is None
    assert out["breakeven_kg_per_year"] is None
    assert "break-even" in out["breakeven_note"].lower()
    assert out["sensitivity"]["yield_plus_20pct_net_usd"] == "-15109.60"


def test_assess_profitable_payback_defined() -> None:
    result = assess(
        "bench", _engineering("bench"),
        _finance(labor_h_per_m2_week="0.1", labor_usd_per_h="15.0"),
    )
    out = result["outputs"]
    assert out["operating_net_usd_per_year"] == "17890.40"
    assert result["verdict"] == "favorable_under_assumptions"
    assert out["simple_payback_years"] == pytest.approx(30000.0 / 17890.4, abs=0.01)


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
