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
    assert out["water_l_per_year"] == 22500.0
    assert out["lighting_kwh_per_year"] == 15330.0
    assert out["water_plus_structure_kg"] == 11500.0


def test_rack3_engineering_hand_check() -> None:
    out = evaluate_scenario("rack3", crop_profile(), _site())["outputs"]
    assert out["sites"] == 7500.0
    assert out["growing_m2"] == 300.0
    assert out["kg_per_year"] == 11250.0
    assert out["water_l_per_year"] == 67500.0
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


def test_run_scenario_persists_behind_fresh_authorization() -> None:
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
    assert stored["dossier_digest"] == "D"
    assert stored["complete"] is True
    assert stored["outputs"]["sites"] == 2500.0
    assert stored["financial_assessment"]["assessed"] is False


def test_run_scenario_records_engineering_and_financial_stages() -> None:
    decision = _record_row(
        "raphael_decision", "d1",
        {"outcome": "authorized", "dossier_key": "main", "dossier_digest": "D"},
        "dd",
    )
    current = _record_row(
        "dossier_version", "main",
        {"property_id": str(uuid4()), "dossier": {
            "county_attribution": {"evidence_status": "verified", "value": "Dallas County"},
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


def test_run_scenario_embeds_michael_assessment() -> None:
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
    assert finance["assessment"]["outputs"]["operating_net_usd_per_year"] == "17890.40"
