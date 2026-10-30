"""Isolated synthetic demonstration harness (MVP fallback path).

Runs the complete Gabriel -> Raphael -> Onesimus software workflow on
explicitly fictional fixtures when authoritative evidence is
unavailable. Isolation guarantees:

- Physically separate in-memory store: no writes to operational
  properties, evidence, dossiers, decisions, quotas, registries, or
  production audit records, and no production database credentials.
- Demo identifiers (``demo-`` prefix) are unmistakable and rejected by
  every production mutation/authorization interface.
- Every artifact, decision, scenario, model result, export, and UI
  response carries the synthetic marker and label.
- Fictional sources (``*.fiction.example``) are never fetched; there
  is no demo-to-production promotion path.
- Verification and authorization here are SIMULATED. Shared pure
  policy/calculation functions (digests, Onesimus math) may be reused;
  their demo results are hypothetical, never production verification.

This module never imports production database helpers and never
performs network I/O.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.agents.onesimus.crops import crop_profile
from app.agents.onesimus.service import (
    ClassifiedInput,
    evaluate_scenario,
)
from app.michael import FinancialInput, assess as michael_assess
from app.workflow import DEMO_ID_PREFIX, digest_payload

DEMO_LABEL = (
    "SYNTHETIC DEMONSTRATION — NOT A REAL PROPERTY; "
    "VERIFICATION AND AUTHORIZATION ARE SIMULATED."
)

DEMO_PROPERTY_NAME = "Fiction Tower (synthetic)"
DEMO_SOURCES = (
    "https://listings.fiction.example/fiction-tower",
    "https://broker.fiction.example/listings/fiction-tower",
)


class DemoError(ValueError):
    """Invalid synthetic-harness request."""


def demo_id(suffix: str) -> str:
    """Build an unmistakable demo-namespace identifier."""
    token = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in suffix)
    return f"{DEMO_ID_PREFIX}{token}"


def mark_synthetic(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the synthetic origin marker (mutates and returns the payload)."""
    payload["synthetic"] = True
    payload["demo_label"] = DEMO_LABEL
    return payload


class DemoStore:
    """In-memory demo-only run/record store (no database, no credentials)."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.records: dict[tuple[str, str, str, int], dict[str, Any]] = {}

    def reset(self) -> None:
        """Clear only this demo store; real records are unreachable here."""
        self.runs.clear()
        self.records.clear()

    def create_run(self, run_key: str) -> dict[str, Any]:
        if not run_key.startswith(DEMO_ID_PREFIX):
            raise DemoError("demo runs require demo-namespace keys")
        run = self.runs.get(run_key)
        if run is None:
            run = mark_synthetic(
                {
                    "run_key": run_key,
                    "stage": "created",
                    "status": "open",
                    "usage": {},
                    "errors": [],
                }
            )
            self.runs[run_key] = run
        return deepcopy(run)

    def put_record(
        self,
        run_key: str,
        kind: str,
        record_key: str,
        version: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if run_key not in self.runs:
            raise DemoError(f"unknown demo run {run_key}")
        if not record_key.startswith(DEMO_ID_PREFIX):
            raise DemoError("demo records require demo-namespace keys")
        digest = f"{DEMO_ID_PREFIX}{digest_payload(payload)}"
        slot = (run_key, kind, record_key, version)
        existing = self.records.get(slot)
        if existing is not None:
            if existing["digest"] != digest:
                raise DemoError(f"conflicting replay of demo {kind}/{record_key}")
            return deepcopy(existing)
        record = {
            "run_key": run_key,
            "kind": kind,
            "record_key": record_key,
            "version": version,
            "payload": mark_synthetic(deepcopy(payload)),
            "digest": digest,
        }
        self.records[slot] = record
        return deepcopy(record)

    def get_record(
        self, run_key: str, kind: str, record_key: str, version: int | None = None
    ) -> dict[str, Any] | None:
        matches = [
            record
            for (run, rec_kind, key, ver), record in self.records.items()
            if run == run_key and rec_kind == kind and key == record_key
            and (version is None or ver == version)
        ]
        if not matches:
            return None
        matches.sort(key=lambda r: r["version"])
        return deepcopy(matches[-1])

    def export_run(self, run_key: str) -> dict[str, Any]:
        """Export one demo run with all records (always labeled synthetic)."""
        run = self.runs.get(run_key)
        if run is None:
            raise DemoError(f"unknown demo run {run_key}")
        records = [
            record
            for (run, _, _, _), record in sorted(self.records.items())
            if run == run_key
        ]
        return mark_synthetic(
            {"run": deepcopy(run), "records": deepcopy(records)}
        )


def demo_gabriel_stage(store: DemoStore, run_key: str) -> dict[str, Any]:
    """Simulated Gabriel stage: fictional evidence, dossier, qualification."""
    store.create_run(run_key)
    artifact_key = demo_id(f"{run_key}-artifact-a1")
    store.put_record(
        run_key, "research_artifact", artifact_key, 1,
        {
            "provider": "fiction-fixture",
            "assignment_id": artifact_key,
            "query": "Fiction Tower Dallas (synthetic query; never executed)",
            "results": [
                {
                    "url": url,
                    "title": "Fiction Tower synthetic listing",
                    "snippet": "Fiction Tower is a 12-story office building (invented).",
                    "collection_provenance": "synthetic_fixture",
                }
                for url in DEMO_SOURCES
            ],
            "collection_provenance": "synthetic_fixture",
            "attempt": "simulated",
        },
    )
    dossier_key = demo_id(f"{run_key}-dossier")
    dossier = {
        "version": 1,
        "builder_version": "demo-fixture-1",
        "artifact_refs": [artifact_key],
        "dossier": {
            "name": DEMO_PROPERTY_NAME,
            "street_address": "1 Fiction Plaza, Dallas, TX (invented)",
            "identity_disputes": {},
            "conflicts": [],
            "category_attribution": {"status": "observed"},
            "sources": list(DEMO_SOURCES),
        },
        "candidate_verification": {
            "outcome": "SIMULATED",
            "note": "simulated verification; not production evidence",
        },
        "property_id": "",
    }
    dossier["digest"] = f"{DEMO_ID_PREFIX}{digest_payload({k: v for k, v in dossier.items() if k != 'digest'})}"
    store.put_record(run_key, "dossier_version", dossier_key, 1, dossier)
    store.put_record(
        run_key, "qualification", demo_id(f"{run_key}-qual"), 1,
        {
            "outcome": "qualified",
            "policy_version": "demo-simulated-1",
            "reasons": ["simulated qualification for demonstration"],
            "evidence_refs": list(DEMO_SOURCES),
            "county_credit": "",
        },
    )
    store.runs[run_key]["stage"] = "gabriel_done"
    record = store.get_record(run_key, "dossier_version", dossier_key)
    assert record is not None
    return record


def demo_raphael_stage(
    store: DemoStore, run_key: str, dossier_key: str, *, simulated_outcome: str = "authorized"
) -> dict[str, Any]:
    """Simulated Raphael stage: a labeled simulated decision (no real gates)."""
    if simulated_outcome not in ("authorized", "needs_evidence", "rejected"):
        raise DemoError(f"unknown simulated outcome: {simulated_outcome}")
    dossier = store.get_record(run_key, "dossier_version", dossier_key)
    if dossier is None:
        raise DemoError(f"unknown demo dossier {dossier_key}")
    decision_key = demo_id(f"{run_key}-decision")
    decision = {
        "outcome": simulated_outcome,
        "simulated": True,
        "dossier_key": dossier_key,
        "dossier_digest": dossier["digest"],
        "basis": [f"simulated {simulated_outcome} for demonstration; not evidence-based"],
        "policy_version": "demo-simulated-1",
        "scope": "Demonstration only. " + DEMO_LABEL,
        "limitations": ["Simulated decision with no evidentiary force."],
    }
    stored = store.put_record(run_key, "raphael_decision", decision_key, 1, decision)
    store.runs[run_key]["stage"] = "raphael_done"
    return stored


def _demo_site_inputs(area_m2: float = 100.0) -> dict[str, ClassifiedInput]:
    def _input(name: str, value: float, unit: str) -> ClassifiedInput:
        return ClassifiedInput(
            name=name, value=value, unit=unit,
            provenance="synthetic_fixture (invented)",
            classification="assumed",
        )

    return {
        "usable_area_m2": _input("usable_area_m2", area_m2, "m2"),
    }


def _demo_financial_inputs(config_name: str) -> dict[str, FinancialInput]:
    from decimal import Decimal

    def _money(name: str, value: str, unit: str, period: str) -> FinancialInput:
        return FinancialInput(
            name=name, value=Decimal(value), unit=unit,
            currency="USD", period=period,
            source="synthetic_fixture (invented)",
            observed_date="", classification="assumed",
            scenario=config_name, uncertainty="invented demo value",
        )

    return {
        "price_per_kg": _money("price_per_kg", "8.0", "USD/kg", "per-kg-saleable"),
        "nutrient_usd_per_l": _money("nutrient_usd_per_l", "0.05", "USD/L", "per-liter"),
        "electricity_usd_per_kwh": _money("electricity_usd_per_kwh", "0.12", "USD/kWh", "per-kWh"),
        "water_usd_per_l": _money("water_usd_per_l", "0.002", "USD/L", "per-liter"),
        "labor_h_per_m2_week": _money("labor_h_per_m2_week", "0.1", "h/m2/week", "per-week"),
        "labor_usd_per_h": _money("labor_usd_per_h", "15.0", "USD/h", "per-hour"),
        "hvac_usd_per_m2_year": _money("hvac_usd_per_m2_year", "8.0", "USD/m2/year", "per-year"),
        "other_opex_usd_per_m2_year": _money("other_opex_usd_per_m2_year", "5.0", "USD/m2/year", "per-year"),
    }


def demo_onesimus_stage(
    store: DemoStore, run_key: str, decision_key: str, config_name: str = "bench"
) -> dict[str, Any]:
    """Simulated Onesimus stage: shared math over invented demo parameters."""
    decision = store.get_record(run_key, "raphael_decision", decision_key)
    if decision is None or not decision["payload"].get("simulated"):
        raise DemoError("demo scenarios require a simulated demo decision")
    if decision["payload"].get("outcome") != "authorized":
        raise DemoError("demo scenario requires a simulated authorized outcome")
    crop = crop_profile()
    for item in crop.values():
        item.provenance = "synthetic_fixture (invented)"
    result = evaluate_scenario(
        config_name, crop, _demo_site_inputs(),
        observed_occupancy="invented: fully vacant (synthetic)",
    )
    finance = michael_assess(
        config_name,
        {
            "complete": result["complete"],
            "missing": result["missing"],
            "outputs": result["outputs"],
        },
        _demo_financial_inputs(config_name),
    )
    payload = {
        "name": demo_id(f"{run_key}-scenario-{config_name}"),
        "property_id": "",
        "dossier_digest": decision["payload"]["dossier_digest"],
        "decision_ref": decision_key,
        **result,
        "financial_assessment": {"assessed": True, "simulated": True, "assessment": finance},
        "note": "Hypothetical results from invented parameters. " + DEMO_LABEL,
    }
    stored = store.put_record(
        run_key, "scenario", demo_id(f"{run_key}-scenario-{config_name}"), 1, payload
    )
    store.runs[run_key]["stage"] = "onesimus_done"
    return stored


def run_demo_workflow(store: DemoStore, run_key: str) -> dict[str, Any]:
    """Run the full synthetic workflow: Gabriel, Raphael, both scenarios."""
    dossier = demo_gabriel_stage(store, run_key)
    decision = demo_raphael_stage(store, run_key, dossier["record_key"])
    bench = demo_onesimus_stage(store, run_key, decision["record_key"], "bench")
    rack = demo_onesimus_stage(store, run_key, decision["record_key"], "rack3")
    store.runs[run_key]["stage"] = "done"
    store.runs[run_key]["status"] = "complete"
    return {
        "run_key": run_key,
        "dossier_digest": dossier["digest"],
        "decision": decision["payload"]["outcome"],
        "bench_net_usd": bench["payload"]["financial_assessment"]["assessment"]["outputs"]["operating_net_usd_per_year"],
        "rack_net_usd": rack["payload"]["financial_assessment"]["assessment"]["outputs"]["operating_net_usd_per_year"],
        "synthetic": True,
        "demo_label": DEMO_LABEL,
    }
