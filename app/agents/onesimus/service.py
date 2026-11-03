"""Onesimus scenario service: deterministic Kratky engineering (MVP).

All agricultural arithmetic lives here in inspectable Python; Michael
owns every financial calculation. AI may propose configurations or
explain results; it must never supply computed outputs.

Two passive, non-circulating Kratky configurations:
- bench: single-level benches, individual containers, no stacking.
- rack3: three-tier stacked racks with shared reservoirs per tier.

Both preserve Kratky principles: static nutrient solution, a root-zone
air gap, no pumps, no circulation. Neither config adds powered
water movement; any override requesting circulation is rejected.

Every consequential input carries value, unit, provenance, and
classification (verified | estimated | experimental | assumed |
unknown). Unknown costs never silently become zero: totals that
depend on unknown inputs are reported incomplete with the missing
quantities named.

Tenant consolidation is an explicit hypothetical assumption
(`tenant_consolidation=True` by default): observed occupancy is
preserved separately, and consent, lease, access, relocation-cost,
and suitability flags are always attached.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.agents.onesimus import ONESIMUS_CALC_VERSION
from app.workflow import KIND_SCENARIO, digest_payload, is_demo_id
from app.workflow_db import (
    WorkflowError,
    get_record_in_transaction,
    put_record_in_transaction,
)

CLASSIFICATIONS = ("verified", "estimated", "experimental", "assumed", "unknown")

TENANT_FLAGS = [
    "Tenant consent not established (hypothetical consolidation).",
    "Lease restructuring unresolved.",
    "Relocation cost not estimated.",
    "Access and operational suitability during conversion unverified.",
]


class ClassifiedInput(BaseModel):
    """One consequential input with unit, provenance, classification."""

    name: str
    value: float | None = Field(default=None, allow_inf_nan=False)
    unit: str = ""
    provenance: str = ""
    classification: str = "unknown"

    @field_validator("value", mode="before")
    @classmethod
    def reject_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("engineering quantity cannot be boolean")
        return value

    @field_validator("classification")
    @classmethod
    def classified(cls, value):
        if value not in CLASSIFICATIONS:
            raise ValueError("unsupported evidence classification")
        return value


class ScenarioConfig(BaseModel):
    """One Kratky configuration definition (physical only; no money)."""

    name: str
    tiers: int = 1
    reservoir: str = "individual"  # individual | shared_per_tier
    structure_kg_per_m2_tier: float = 0.0
    circulation: bool = False  # must stay False: passive Kratky only


CONFIGS: dict[str, ScenarioConfig] = {
    "bench": ScenarioConfig(
        name="bench",
        tiers=1,
        reservoir="individual",
        structure_kg_per_m2_tier=15.0,
    ),
    "rack3": ScenarioConfig(
        name="rack3",
        tiers=3,
        reservoir="shared_per_tier",
        structure_kg_per_m2_tier=25.0,
    ),
}

# Approved tunable catalog for model-proposed comparisons: engineering
# parameters only. Financial tunables live in Michael's catalog.
# Overrides outside these names/ranges are rejected; Python computes.
PARAMETER_CATALOG: dict[str, dict[str, Any]] = {
    "config": {"allowed": ["bench", "rack3"]},
    "usable_area_m2": {"min": 10.0, "max": 100000.0},
    "cycles_per_year": {"min": 1.0, "max": 15.0},
    "tenant_consolidation": {"allowed": [True, False]},
}


def evaluate_scenario(
    config_name: str,
    crop: dict[str, ClassifiedInput],
    site: dict[str, ClassifiedInput],
    *,
    tenant_consolidation: bool = True,
    observed_occupancy: str = "",
) -> dict[str, Any]:
    """Deterministic scenario evaluation (pure; no I/O, no AI calls)."""
    config = CONFIGS.get(config_name)
    if config is None:
        raise WorkflowError(f"unknown scenario config: {config_name}")
    if config.circulation:
        raise WorkflowError("circulation violates passive Kratky principles")
    from app.agents.onesimus.engineering import calculate
    return calculate(config, crop, site, tenant_consolidation=tenant_consolidation,
                     observed_occupancy=observed_occupancy, tenant_flags=TENANT_FLAGS,
                     calculation_version=ONESIMUS_CALC_VERSION)


def validate_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    """Validate model-proposed tunables against the approved catalog."""
    cleaned: dict[str, Any] = {}
    for name, value in overrides.items():
        rule = PARAMETER_CATALOG.get(name)
        if rule is None:
            raise WorkflowError(f"parameter not in approved catalog: {name}")
        if "allowed" in rule:
            if value not in rule["allowed"]:
                raise WorkflowError(f"parameter {name} value not allowed")
            cleaned[name] = value
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise WorkflowError(f"parameter {name} must be numeric")
        if not (float(rule["min"]) <= number <= float(rule["max"])):
            raise WorkflowError(f"parameter {name} out of catalog range")
        cleaned[name] = number
    return cleaned


def _is_demo_digest(digest: str) -> bool:
    return is_demo_id(digest)


def _require_engineering_custody(conn, current, site):
    from app.custody import dossier_id_for_property
    from app.custody_db import (current_custody_in_transaction, verify_chain_in_transaction,
                                latest_version_in_transaction, lock_dossier_in_transaction)
    property_id = current.payload.get("property_id")
    if not property_id:
        raise WorkflowError("engineering dossier has no operational property identity")
    dossier_id = dossier_id_for_property(property_id)
    lock_dossier_in_transaction(conn, dossier_id)
    verify_chain_in_transaction(conn, dossier_id)
    custody = current_custody_in_transaction(conn, dossier_id)
    if not custody or custody["holder"] != "onesimus":
        raise WorkflowError("Onesimus must hold authenticated dossier custody")
    latest = latest_version_in_transaction(conn, dossier_id)
    if (latest is None or custody["version"] != latest.version
            or latest.payload.get("dossier_digest") != current.digest
            or latest.payload.get("dossier") != current.payload.get("dossier")):
        raise WorkflowError("engineering input is not the latest received custody version")
    inspection = (current.payload.get("dossier", {}).get("reports") or {}).get("raphael")
    if not inspection:
        raise WorkflowError("engineering requires a frozen inspection report")
    declared = inspection.get("physical_inputs") or {}
    from app.agents.onesimus.engineering import SITE_UNITS
    expected = {k: ClassifiedInput.model_validate(v) for k,v in declared.items() if k in SITE_UNITS}
    actual = {k:v for k,v in site.items() if v.value is not None}
    expected = {k:v for k,v in expected.items() if v.value is not None}
    if actual != expected:
        raise WorkflowError("engineering physical inputs differ from the inspected dossier")


def run_scenario_in_transaction(
    conn: Any,
    run_id: UUID,
    *,
    scenario_key: str,
    config_name: str,
    crop: dict[str, ClassifiedInput],
    site: dict[str, ClassifiedInput],
    decision_digest: str,
    tenant_consolidation: bool = True,
    observed_occupancy: str = "",
    financial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate and persist one scenario behind a validated authorization.

    The referenced Raphael decision must exist, be `authorized`, and
    bind the run's current dossier version digest. Demo-only digests,
    missing decisions, stale digests, and mismatches are rejected.

    When a financial bundle is supplied, Michael's assessment is
    embedded under `financial_assessment`; otherwise that section is
    marked pending. Pilot stage counters record engineering/financial
    outcomes when the authorized dossier carries a known cell.
    """
    if _is_demo_digest(decision_digest):
        raise WorkflowError("demo-only authorization cannot run real scenarios")
    decision = get_record_in_transaction(conn, run_id, "raphael_decision", decision_digest)
    if decision is None:
        raise WorkflowError("missing Raphael authorization for scenario")
    if decision.payload.get("outcome") != "authorized":
        raise WorkflowError(
            f"Raphael decision is {decision.payload.get('outcome')}, not authorized"
        )
    # Bind to the exact dossier digest the decision authorized: a newer
    # dossier version invalidates reuse of the old authorization.
    dossier_key = decision.payload.get("dossier_key", "")
    current = get_record_in_transaction(conn, run_id, "dossier_version", dossier_key) if dossier_key else None
    authorized_digest = decision.payload.get("dossier_digest", "")
    if current is None or not authorized_digest:
        raise WorkflowError("missing current dossier for Raphael authorization")
    if current.digest != authorized_digest:
        raise WorkflowError("dossier revised since authorization; re-authorization required")
    if current.digest != digest_payload({k: v for k, v in current.payload.items() if k != "digest"}):
        raise WorkflowError("authorized dossier digest failed verification")
    if decision.digest != digest_payload(decision.payload):
        raise WorkflowError("Raphael decision digest failed verification")
    _require_engineering_custody(conn, current, site)
    result = evaluate_scenario(
        config_name, crop, site,
        tenant_consolidation=tenant_consolidation,
        observed_occupancy=observed_occupancy,
    )
    from app.michael import assess as michael_assess

    finance_section: dict[str, Any]
    if financial is None:
        finance_section = {
            "assessed": False,
            "reason": "no financial bundle supplied; Michael assessment pending",
        }
    else:
        finance_section = {
            "assessed": True,
            "assessment": michael_assess(
                config_name,
                {
                    "complete": result["complete"],
                    "missing": result["missing"],
                    "outputs": result["outputs"],
                },
                financial,
            ),
        }
    payload = {
        "name": scenario_key,
        "property_id": str(current.payload.get("property_id") or ""),
        "dossier_digest": authorized_digest,
        "decision_ref": decision_digest,
        **result,
        "financial_assessment": finance_section,
    }
    stored = put_record_in_transaction(
        conn, run_id, kind=KIND_SCENARIO, record_key=scenario_key,
        version=1, payload=payload, digest=digest_payload(payload),
    )
    # Pilot stage counters: keyed by the canonical operational dossier
    # identity of the authorized dossier, so repeated scenarios and
    # cross-run rediscovery cannot inflate the counts.
    if dossier_key and current is not None:
        from app.pilot import (
            PilotError,
            canonical_quota_id,
            cell_from_dossier,
            record_transition_in_transaction,
        )

        dossier_dump = (current.payload or {}).get("dossier", {}) or {}
        cell = cell_from_dossier(dossier_dump)
        if cell is not None:
            try:
                quota_id = canonical_quota_id(
                    str((current.payload or {}).get("property_id", "") or "")
                )
            except PilotError:
                quota_id = None
            if quota_id is not None:
                county, category = cell
                record_transition_in_transaction(
                    conn, county=county, category=category, dossier_id=quota_id,
                    transition="engineering_assessed" if result["complete"] else "incomplete",
                    run_id=run_id, record_key=dossier_key,
                )
                if finance_section.get("assessed"):
                    assessment = finance_section.get("assessment", {})
                    record_transition_in_transaction(
                        conn, county=county, category=category, dossier_id=quota_id,
                        transition=(
                            "financially_assessed" if assessment.get("complete") else "incomplete"
                        ),
                        run_id=run_id, record_key=dossier_key,
                    )
    return stored.payload
