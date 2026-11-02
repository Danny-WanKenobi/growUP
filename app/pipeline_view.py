"""Canonical pipeline summary for the Wondrue cockpit (read-only).

Composes the shared property record across Gabriel, Raphael,
Onesimus, Michael, Uriel, and Priscilla into one display-ready
summary. This module only *presents* committed stage outputs: it
never recomputes, authorizes, or upgrades anything. Absent stages
are reported as not_recorded (stale/unavailable information is
named, never smoothed over), simulated and actual conversion
counts are carried separately, and a speculative scenario can
never surface as a verified opportunity.
"""

from __future__ import annotations

from typing import Any

STAGE_ORDER = (
    "gabriel", "raphael", "onesimus", "michael", "uriel", "priscilla",
)


def _stage(status: str, **fields: Any) -> dict[str, Any]:
    return {"status": status, **fields}


def build_pipeline_summary(stages: dict[str, Any]) -> dict[str, Any]:
    """Build the cockpit summary from committed stage payloads.

    ``stages`` maps stage names to their committed payloads (or
    None/absent when nothing is recorded). Payload shapes are the
    agents' own outputs; unknown fields pass through untouched.
    """
    stages = stages or {}
    dossier = stages.get("gabriel") or {}
    verification = dossier.get("candidate_verification") or {}
    raphael = stages.get("raphael") or {}
    engineering = stages.get("onesimus") or {}
    finance = stages.get("michael") or {}
    assessment = finance.get("assessment") or finance
    uriel = stages.get("uriel") or {}
    priscilla = stages.get("priscilla") or {}
    metrics = priscilla.get("metrics") or {}

    view: dict[str, dict[str, Any]] = {}
    view["gabriel"] = _stage(
        "recorded" if dossier else "not_recorded",
        dossier_key=dossier.get("key"),
        category_attribution=dossier.get("category_attribution"),
        verification_outcome=verification.get("outcome"),
        verification_pathway=verification.get("pathway"),
    )
    view["raphael"] = _stage(
        "recorded" if raphael else "not_recorded",
        outcome=raphael.get("outcome"),
        basis=raphael.get("basis"),
    )
    view["onesimus"] = _stage(
        "recorded" if engineering else "not_recorded",
        complete=engineering.get("complete"),
        missing=engineering.get("missing"),
        not_quantified=engineering.get("not_quantified"),
        outputs=engineering.get("outputs"),
        label="preliminary engineering scenario",
    )
    view["michael"] = _stage(
        "recorded" if finance else "not_recorded",
        complete=assessment.get("complete"),
        verdict=assessment.get("verdict"),
        outputs=assessment.get("outputs"),
        evidence_requests=assessment.get("evidence_requests"),
        label=(
            "conditional feasibility under stated assumptions; "
            "never a verified opportunity"
        ),
    )
    view["uriel"] = _stage(
        "recorded" if uriel else "not_recorded",
        readiness=uriel.get("readiness"),
        score=uriel.get("score"),
        package_state=uriel.get("package_state"),
        missing_data=uriel.get("missing_data"),
    )
    simulated = metrics.get("simulated") or {}
    actual = metrics.get("actual") or {}
    view["priscilla"] = _stage(
        "recorded" if priscilla else "not_recorded",
        simulated_events=simulated.get("total"),
        actual_events=actual.get("total"),
        note=(
            "simulated and actual events are tracked separately and "
            "are never combined into one conversion count"
        ),
    )

    gaps: list[str] = []
    for name in STAGE_ORDER:
        stage = view[name]
        if stage["status"] == "not_recorded":
            gaps.append(f"{name}: nothing recorded")
    for item in engineering.get("missing") or []:
        gaps.append(f"onesimus missing input: {item}")
    for item in assessment.get("evidence_requests") or []:
        gaps.append(f"michael evidence request: {item}")
    if verification.get("outcome") not in (None, "verified"):
        gaps.append(
            f"category verification: {verification.get('outcome')}"
        )
    return {
        "stages": [dict(name=name, **view[name]) for name in STAGE_ORDER],
        "evidence_gaps": gaps,
        "truthfulness": (
            "verified facts, estimates, assumptions, and unknowns "
            "keep their recorded labels at every stage"
        ),
    }
