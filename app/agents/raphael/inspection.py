"""Evidence-based preliminary inspection report; never a building approval."""
from copy import deepcopy
from typing import Any

from app.agents.onesimus.engineering import SITE_UNITS, quantity
from app.agents.onesimus.service import ClassifiedInput
from app.workflow import digest_payload


def build_inspection_report(payload: dict[str, Any], *, site_inputs=None, decision=None):
    from app.agents.raphael.service import evaluate_dossier
    dossier = payload.get("dossier") or {}
    verdict = decision or evaluate_dossier(payload)
    physical = {}
    for name, unit in SITE_UNITS.items():
        raw = (site_inputs or {}).get(name)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"{name} requires a classified input object")
        supplied = ClassifiedInput.model_validate({"name": name, "unit": unit,
            "classification": "assumed" if raw.get("value") is not None else "unknown", **raw})
        # Operator inputs are scenario assumptions. A string claiming
        # 'verified' is not authenticated evidence of building capacity.
        classification = supplied.classification
        if classification == "verified":
            classification = "assumed"
        normalized = quantity({name: supplied}, name, SITE_UNITS)
        physical[name] = {"name": name, "value": normalized, "unit": unit,
                          "provenance": supplied.provenance or "operator scenario assumption; not measured by Raphael",
                          "classification": classification,
                          "declared_classification": supplied.classification}
    unknowns = [name for name in ("room_length_m", "room_width_m", "clear_height_m")
                if physical.get(name, {}).get("value") is None]
    facts = {}
    for name in ("structural_capacity", "electrical_capacity", "water_quality",
                 "permitted_agricultural_use", "actual_vacancy", "zoning_permission"):
        facts[name] = {"value": None, "classification": "unknown", "evidence_refs": [],
                       "reason": "No authenticated assessment supplied for this fact."}
    report = {
        "schema_version": "raphael-inspection-1",
        "source_dossier_digest": str(payload.get("digest") or digest_payload(payload)),
        "outcome": verdict["outcome"], "reasons": list(verdict.get("basis") or []),
        "physical_inputs": physical,
        "engineering_options": deepcopy((site_inputs or {}).get("engineering_options") or {}),
        "dimensions": {k: deepcopy(v) for k, v in physical.items()
                       if k in ("usable_area_m2", "room_length_m", "room_width_m", "clear_height_m")},
        "facts": facts, "evidence_refs": deepcopy(dossier.get("sources") or []),
        "observed_occupancy": deepcopy(dossier.get("occupancy") or dossier.get("occupancy_status") or ""),
        "constraints": ["No on-site structural, electrical or code inspection has been established.",
                        "Source claims and operator assumptions are not verified capacity.",
                        "Tenant consent, leases and any relocation remain unresolved."],
        "evidence_requests": [f"provide sourced {name}" for name in unknowns] +
                             [f"obtain qualified review of {name}" for name in facts],
        "scope": "Preliminary evidence assessment only; no construction or operational approval.",
    }
    report["digest"] = digest_payload(report)
    return report
