"""Onesimus deterministic engineering tests (pure calculations, no PG)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.agents.onesimus.crops import crop_profile
from app.agents.onesimus.service import (
    CONFIGS,
    ClassifiedInput,
    evaluate_scenario,
    run_scenario_in_transaction,
    validate_overrides,
)
from app.michael import financial_inputs_from_raw
from app.workflow_db import WorkflowError


class _FakeCursor:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        if not self._script:
            return None
        item = self._script.pop(0)
        if item == "ECHO_INSERT":
            import json as _json
            from uuid import uuid4 as _uuid4

            params = self.executed[-1][1]
            payload = params.get("payload")
            if isinstance(payload, str):
                payload = _json.loads(payload)
            return {
                "record_id": _uuid4(),
                "run_id": params.get("run"),
                "kind": params.get("kind"),
                "record_key": params.get("key"),
                "version": params.get("version", 1),
                "payload": payload,
                "digest": params.get("digest"),
            }
        return item

    def fetchall(self) -> Any:
        return self._script.pop(0) if self._script else []


class _FakeConn:
    def __init__(self, script: list[Any]) -> None:
        # Valid authorization fixtures carry real hashes; stale test tags
        # deliberately remain different. No database is involved.
        from app.workflow import digest_payload
        if len(script) > 1 and isinstance(script[0], dict) and isinstance(script[1], dict):
            decision, dossier = script[:2]
            if decision.get("kind") == "raphael_decision" and dossier.get("kind") == "dossier_version":
                original = dossier["digest"]
                dossier["digest"] = digest_payload({k:v for k,v in dossier["payload"].items() if k!="digest"})
                if decision["payload"].get("dossier_digest") == original:
                    decision["payload"]["dossier_digest"] = dossier["digest"]
                decision["digest"] = digest_payload(decision["payload"])
        self._cursor = _FakeCursor(script)

    def cursor(self) -> _FakeCursor:
        return self._cursor


def _site(**overrides: Any) -> dict[str, ClassifiedInput]:
    values = {"usable_area_m2": 100.0}
    values.update(overrides)
    return {
        name: ClassifiedInput(
            name=name, value=value, unit="m2",
            provenance="test", classification="assumed",
        )
        for name, value in values.items()
    }


def test_bench_engineering_hand_check() -> None:
    result = evaluate_scenario("bench", crop_profile(), _site())
    assert result["complete"] is True
    out = result["outputs"]
    assert out["sites"] == 2500.0
    assert out["growing_m2"] == 100.0
    assert out["heads_per_year"] == 25000.0
    assert out["kg_per_year"] == 3750.0
    assert out["reservoir_l_total"] == 10000.0
    assert out["water_l_per_year"] == 112500.0
    assert out["lighting_kwh_per_year"] == 15330.0
    assert out["water_plus_structure_kg"] == 11500.0


def test_rack3_engineering_hand_check() -> None:
    out = evaluate_scenario("rack3", crop_profile(), _site())["outputs"]
    assert out["sites"] == 7500.0
    assert out["growing_m2"] == 300.0
    assert out["kg_per_year"] == 11250.0
    assert out["water_l_per_year"] == 337500.0
    assert out["water_plus_structure_kg"] == 37500.0


def test_engineering_carries_no_money() -> None:
    result = evaluate_scenario("bench", crop_profile(), _site())
    for name in result["outputs"]:
        assert "usd" not in name and "net" not in name and "payback" not in name
    assert any("Michael" in line for line in result["limitations"])
    assert "price_per_kg" not in result["inputs"]
    assert "nutrient_usd_per_l" not in result["inputs"]


def test_missing_physical_input_is_incomplete() -> None:
    site = _site()
    del site["usable_area_m2"]
    result = evaluate_scenario("bench", crop_profile(), site)
    assert result["complete"] is False
    assert result["missing"] == ["usable_area_m2"]
    assert result["outputs"] == {}


def test_none_crop_value_named_missing() -> None:
    crop = crop_profile()
    crop["spacing_m"] = ClassifiedInput(
        name="spacing_m", value=None, unit="m",
        provenance="test", classification="unknown",
    )
    result = evaluate_scenario("bench", crop, _site())
    assert result["complete"] is False
    assert "spacing_m" in result["missing"]


def test_both_configs_preserve_passive_kratky() -> None:
    assert set(CONFIGS) == {"bench", "rack3"}
    for config in CONFIGS.values():
        assert config.circulation is False
    with pytest.raises(WorkflowError, match="unknown scenario config"):
        evaluate_scenario("aeroponic", crop_profile(), _site())


def test_tenant_consolidation_hypothetical_and_flagged() -> None:
    result = evaluate_scenario(
        "bench", crop_profile(), _site(), observed_occupancy="2 of 5 floors leased"
    )
    assert result["observed_occupancy"] == "2 of 5 floors leased"
    assert len(result["tenant_flags"]) == 4
    assert any("consent" in flag for flag in result["tenant_flags"])
    off = evaluate_scenario("bench", crop_profile(), _site(), tenant_consolidation=False)
    assert off["tenant_flags"] == []


def test_all_inputs_classified() -> None:
    result = evaluate_scenario("bench", crop_profile(), _site())
    for name, item in result["inputs"].items():
        assert item["classification"] in (
            "verified", "estimated", "experimental", "assumed", "unknown",
        ), name
        assert item["unit"], name
        assert item["provenance"], name


def test_validate_overrides_engineering_catalog_only() -> None:
    assert validate_overrides({"config": "rack3", "cycles_per_year": 11.0}) == {
        "config": "rack3", "cycles_per_year": 11.0,
    }
    with pytest.raises(WorkflowError, match="not in approved catalog"):
        validate_overrides({"price_per_kg": 9.5})  # Michael's catalog, not Onesimus's
    with pytest.raises(WorkflowError, match="out of catalog range"):
        validate_overrides({"usable_area_m2": 500000.0})
    with pytest.raises(WorkflowError, match="not allowed"):
        validate_overrides({"config": "aeroponic"})


def _record_row(kind: str, key: str, payload: dict[str, Any], digest: str) -> dict[str, Any]:
    return {
        "record_id": uuid4(), "run_id": uuid4(), "kind": kind,
        "record_key": key, "version": 1, "payload": payload, "digest": digest,
    }


def test_run_scenario_rejects_demo_authorization() -> None:
    conn = _FakeConn([])
    with pytest.raises(WorkflowError, match="demo-only"):
        run_scenario_in_transaction(
            conn, uuid4(), scenario_key="s1", config_name="bench",
            crop=crop_profile(), site=_site(), decision_digest="demo-abc123",
        )
    assert conn._cursor.executed == []


def test_run_scenario_rejects_missing_decision() -> None:
    conn = _FakeConn([None])
    with pytest.raises(WorkflowError, match="missing Raphael authorization"):
        run_scenario_in_transaction(
            conn, uuid4(), scenario_key="s1", config_name="bench",
            crop=crop_profile(), site=_site(), decision_digest="d1",
        )


def test_run_scenario_rejects_non_authorized_decision() -> None:
    decision = _record_row(
        "raphael_decision", "d1",
        {"outcome": "needs_evidence", "dossier_key": "main", "dossier_digest": "D"},
        "dd",
    )
    conn = _FakeConn([decision])
    with pytest.raises(WorkflowError, match="not authorized"):
        run_scenario_in_transaction(
            conn, uuid4(), scenario_key="s1", config_name="bench",
            crop=crop_profile(), site=_site(), decision_digest="d1",
        )


def test_run_scenario_rejects_stale_authorization() -> None:
    decision = _record_row(
        "raphael_decision", "d1",
        {"outcome": "authorized", "dossier_key": "main", "dossier_digest": "D-old"},
        "dd",
    )
    current = _record_row("dossier_version", "main", {"dossier": {}}, "D-new")
    conn = _FakeConn([decision, current])
    with pytest.raises(WorkflowError, match="re-authorization required"):
        run_scenario_in_transaction(
            conn, uuid4(), scenario_key="s1", config_name="bench",
            crop=crop_profile(), site=_site(), decision_digest="d1",
        )


def test_run_scenario_persists_behind_fresh_authorization(monkeypatch) -> None:
    # Custody itself is exercised by the real coordinator/funnel tests.
    monkeypatch.setattr("app.agents.onesimus.service._require_engineering_custody", lambda *a: None)
    decision = _record_row(
        "raphael_decision", "d1",
        {"outcome": "authorized", "dossier_key": "main", "dossier_digest": "D"},
        "dd",
    )
    current = _record_row("dossier_version", "main", {"dossier": {}}, "D")
    conn = _FakeConn([decision, current, "ECHO_INSERT"])
    stored = run_scenario_in_transaction(
        conn, uuid4(), scenario_key="s1", config_name="bench",
        crop=crop_profile(), site=_site(), decision_digest="d1",
    )
    assert stored["decision_ref"] == "d1"
    assert stored["dossier_digest"] == current["digest"]
    assert stored["complete"] is True
    assert stored["outputs"]["sites"] == 2500.0
    assert stored["financial_assessment"]["assessed"] is False


def test_run_scenario_records_engineering_and_financial_stages(monkeypatch) -> None:
    # Custody itself is exercised by the real coordinator/funnel tests.
    monkeypatch.setattr("app.agents.onesimus.service._require_engineering_custody", lambda *a: None)
    decision = _record_row(
        "raphael_decision", "d1",
        {"outcome": "authorized", "dossier_key": "main", "dossier_digest": "D"},
        "dd",
    )
    current = _record_row(
        "dossier_version", "main",
        {"property_id": str(uuid4()), "dossier": {
            "county_attribution": {
                "evidence_status": "verified", "value": "dallas county",
                "claims": [{
                    "original": "Dallas County", "normalized": "dallas county",
                    "source_url": "https://geocoding.geo.census.gov/geocoder/geographies/address",
                    "authority": "census_geocoder",
                }],
            },
            "category_attribution": {"status": "observed", "observed": "high_rise"},
        }},
        "D",
    )
    conn = _FakeConn([
        decision, current, "ECHO_INSERT",
        {"county": "dallas county"}, {"event_id": uuid4()},
        {"county": "dallas county"}, {"event_id": uuid4()},
    ])
    bundle = financial_inputs_from_raw(
        {
            "price_per_kg": {"value": 8.0},
            "nutrient_usd_per_l": {"value": 0.05},
            "electricity_usd_per_kwh": {"value": 0.12},
            "water_usd_per_l": {"value": 0.002},
            "labor_h_per_m2_week": {"value": 0.1},
            "labor_usd_per_h": {"value": 15.0},
            "hvac_usd_per_m2_year": {"value": 8.0},
            "other_opex_usd_per_m2_year": {"value": 5.0},
        },
        scenario="bench",
    )
    stored = run_scenario_in_transaction(
        conn, uuid4(), scenario_key="s1", config_name="bench",
        crop=crop_profile(), site=_site(), decision_digest="d1",
        financial=bundle,
    )
    assert stored["financial_assessment"]["assessment"]["complete"] is True
    transitions = [
        params["transition"] for sql, params in conn._cursor.executed
        if "INSERT INTO pilot_events" in sql
    ]
    assert transitions == ["engineering_assessed", "financially_assessed"]


def test_run_scenario_embeds_michael_assessment(monkeypatch) -> None:
    # Custody itself is exercised by the real coordinator/funnel tests.
    monkeypatch.setattr("app.agents.onesimus.service._require_engineering_custody", lambda *a: None)
    decision = _record_row(
        "raphael_decision", "d1",
        {"outcome": "authorized", "dossier_key": "main", "dossier_digest": "D"},
        "dd",
    )
    current = _record_row("dossier_version", "main", {"dossier": {}}, "D")
    conn = _FakeConn([decision, current, "ECHO_INSERT"])
    bundle = financial_inputs_from_raw(
        {
            "price_per_kg": {"value": 8.0},
            "nutrient_usd_per_l": {"value": 0.05},
            "electricity_usd_per_kwh": {"value": 0.12},
            "water_usd_per_l": {"value": 0.002},
            "labor_h_per_m2_week": {"value": 0.1},
            "labor_usd_per_h": {"value": 15.0},
            "hvac_usd_per_m2_year": {"value": 8.0},
            "other_opex_usd_per_m2_year": {"value": 5.0},
        },
        scenario="bench",
    )
    stored = run_scenario_in_transaction(
        conn, uuid4(), scenario_key="s1", config_name="bench",
        crop=crop_profile(), site=_site(), decision_digest="d1",
        financial=bundle,
    )
    finance = stored["financial_assessment"]
    assert finance["assessed"] is True
    assert finance["assessment"]["complete"] is True
    assert finance["assessment"]["outputs"]["operating_net_usd_per_year"] == "13210.40"


def test_optional_physical_quantifications_flagged_not_zeroed() -> None:
    result = evaluate_scenario("bench", crop_profile(), _site())
    assert result["not_quantified"] == [
        "cleaning_water", "crop_light_delivery", "hvac_energy", "nutrient_volume", "labor_hours"]
    assert "hvac_kwh_per_year" not in result["outputs"]

    crop = crop_profile()
    crop["nutrient_ml_per_site_cycle"] = ClassifiedInput(
        name="nutrient_ml_per_site_cycle", value=100.0, unit="mL/site/cycle",
        provenance="test", classification="experimental")
    crop["labor_min_per_site_cycle"] = ClassifiedInput(
        name="labor_min_per_site_cycle", value=2.0, unit="min/site/cycle",
        provenance="test", classification="experimental")
    site = _site()
    site["hvac_w_per_m2"] = ClassifiedInput(
        name="hvac_w_per_m2", value=10.0, unit="W/m2",
        provenance="test", classification="assumed")
    result = evaluate_scenario("bench", crop, site)
    assert result["not_quantified"] == ["cleaning_water", "crop_light_delivery"]
    out = result["outputs"]
    assert out["nutrient_l_per_year"] == 2500.0
    assert out["labor_hours_per_year"] == pytest.approx(833.3, abs=0.1)
    assert out["hvac_kwh_per_year"] == 8760.0
