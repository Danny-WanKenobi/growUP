"""Gabriel stage finalization: persist research artifacts, dossier versions,
and deterministic qualifications for one workflow run.

Pure persistence over server-owned objects: research results as
collected, dossiers as built, and one qualification verdict per
dossier. Qualification is deterministic and separate from QC/ranking
(the candidate's own assessment) and from Raphael authorization (a
later stage over the persisted version).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.workflow import (
    KIND_DOSSIER_VERSION,
    KIND_QUALIFICATION,
    KIND_RESEARCH_ARTIFACT,
    digest_payload,
)
from app.workflow_db import (
    WorkflowError,
    get_record_in_transaction,
    put_record_in_transaction,
)

GABRIEL_FINALIZE_VERSION = "mvp-1"
QUALIFICATION_POLICY_VERSION = "mvp-qual-1"


def _result_payload(item: Any) -> dict[str, Any]:
    dumped = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
    return {
        "url": dumped.get("url", ""),
        "title": dumped.get("title", ""),
        "snippet": dumped.get("snippet", ""),
        "domain": dumped.get("domain", ""),
        "published_at": dumped.get("published_at"),
        "retrieved_at": dumped.get("retrieved_at", ""),
        "rank": dumped.get("rank"),
        "assignment_id": dumped.get("assignment_id", ""),
    }


def persist_research_artifacts(
    conn: Any, run_id: UUID, research_result: Any
) -> list[str]:
    """Persist one artifact per executed assignment; return artifact keys."""
    executed = getattr(research_result, "executed", []) or []
    results = getattr(research_result, "results", []) or []
    errors = {e.assignment_id: e.error for e in (getattr(research_result, "errors", []) or [])}
    by_assignment: dict[str, list[Any]] = {}
    for item in results:
        by_assignment.setdefault(getattr(item, "assignment_id", ""), []).append(item)
    keys: list[str] = []
    for assignment in executed:
        assignment_id = getattr(assignment, "assignment_id", "") or getattr(assignment, "id", "")
        if get_record_in_transaction(conn, run_id, KIND_RESEARCH_ARTIFACT, assignment_id) is not None:
            keys.append(assignment_id)  # idempotent resume: keep existing artifact
            continue
        payload = {
            "provider": "brave",
            "assignment_id": assignment_id,
            "query": getattr(assignment, "query", ""),
            "results": [_result_payload(item) for item in by_assignment.get(assignment_id, [])],
            "retrieved_at": getattr(research_result, "retrieved_at", ""),
            "collection_provenance": "adapter_collected",
            "attempt": "completed",
            "error": "",
        }
        put_record_in_transaction(
            conn, run_id, kind=KIND_RESEARCH_ARTIFACT, record_key=assignment_id,
            version=1, payload=payload, digest=digest_payload(payload),
        )
        keys.append(assignment_id)
    for assignment_id, code in errors.items():
        if assignment_id in keys:
            continue
        payload = {
            "provider": "brave",
            "assignment_id": assignment_id,
            "query": "",
            "results": [],
            "retrieved_at": getattr(research_result, "retrieved_at", ""),
            "collection_provenance": "adapter_collected",
            "attempt": "failed",
            "error": str(code),
        }
        put_record_in_transaction(
            conn, run_id, kind=KIND_RESEARCH_ARTIFACT, record_key=assignment_id,
            version=1, payload=payload, digest=digest_payload(payload),
        )
        keys.append(assignment_id)
    return keys


def _candidate_verification(ranking: Any, key: str) -> dict[str, Any] | None:
    if ranking is None:
        return None
    for candidate in getattr(ranking, "candidates", []) or []:
        if getattr(candidate, "key", None) == key:
            verification = getattr(candidate, "category_verification", None)
            if verification is None:
                return None
            return verification.model_dump(mode="json")
    return None


def persist_dossier_versions(
    conn: Any,
    run_id: UUID,
    dossier_set: Any,
    ranking: Any,
    artifact_keys: list[str],
    property_ids: dict[str, str] | None = None,
) -> list[str]:
    """Persist one immutable dossier version per dossier; return digests."""
    resolved = property_ids or {}
    digests: list[str] = []
    for dossier in getattr(dossier_set, "dossiers", []) or []:
        key = getattr(dossier, "key", "")
        payload = {
            "version": 1,
            "builder_version": getattr(dossier_set, "dossier_version", ""),
            "artifact_refs": list(artifact_keys),
            "dossier": dossier.model_dump(mode="json"),
            "candidate_verification": _candidate_verification(ranking, key),
            "property_id": resolved.get(key, ""),
        }
        payload["digest"] = digest_payload(
            {k: v for k, v in payload.items() if k != "digest"}
        )
        put_record_in_transaction(
            conn, run_id, kind=KIND_DOSSIER_VERSION, record_key=key,
            version=1, payload=payload, digest=str(payload["digest"]),
        )
        _record_pilot_stage(
            conn, run_id, key, dict(payload.get("dossier", {}) or {}),
            "discovered", resolved.get(key, ""),
        )
        digests.append(str(payload["digest"]))
    return digests


def qualify_dossier(dossier: dict[str, Any]) -> dict[str, Any]:
    """Deterministic research qualification for one dossier payload.

    Qualified means research-complete enough for Raphael review — not
    authorized. Unknown county stays researchable but earns no
    county-cell credit (county_credit "").
    """
    reasons: list[str] = []
    disputes = dossier.get("identity_disputes") or {}
    if disputes:
        reasons.append(f"identity disputes: {sorted(disputes)}")
    conflicts = dossier.get("conflicts") or []
    if conflicts:
        reasons.append(f"{len(conflicts)} unresolved conflict(s)")
    category = dossier.get("category_attribution") or {}
    category_status = str(category.get("status", "unknown"))
    if category_status in ("unknown", ""):
        reasons.append("category unknown")
    if category_status == "contradictory":
        return {
            "outcome": "not_qualified",
            "policy_version": QUALIFICATION_POLICY_VERSION,
            "reasons": reasons + ["category contradictory"],
            "evidence_refs": list(dossier.get("sources") or []),
            "county_credit": "",
        }
    if reasons:
        return {
            "outcome": "needs_research",
            "policy_version": QUALIFICATION_POLICY_VERSION,
            "reasons": reasons,
            "evidence_refs": list(dossier.get("sources") or []),
            "county_credit": "",
        }
    county = dossier.get("county_attribution") or {}
    verified_county = ""
    if str(county.get("evidence_status", "")).lower() == "verified":
        verified_county = str(county.get("value", "") or "")
    return {
        "outcome": "qualified",
        "policy_version": QUALIFICATION_POLICY_VERSION,
        "reasons": ["research complete; awaiting Raphael authorization"],
        "evidence_refs": list(dossier.get("sources") or []),
        "county_credit": verified_county,
    }


def _record_pilot_stage(
    conn: Any,
    run_id: UUID,
    dossier_key: str,
    dossier: dict[str, Any],
    transition: str,
    property_id: str = "",
) -> None:
    """Record a pilot stage transition when cell and identity are known.

    Unknown county, unattributable category, or an unresolved
    property records nothing: the dossier stays researchable but
    earns no quota credit. Quota identity is the canonical
    operational dossier UUID; the run ID is provenance only. Pilot
    ledger absence (migration 014 not applied) is a configuration
    error and propagates rather than silently dropping counts.
    """
    from app.pilot import (
        PilotError,
        canonical_quota_id,
        cell_from_dossier,
        record_transition_in_transaction,
    )

    cell = cell_from_dossier(dossier)
    if cell is None:
        return
    try:
        quota_id = canonical_quota_id(property_id)
    except PilotError:
        return
    county, category = cell
    record_transition_in_transaction(
        conn, county=county, category=category,
        dossier_id=quota_id,
        transition=transition,
        run_id=run_id,
        record_key=dossier_key,
    )


def persist_qualifications(conn: Any, run_id: UUID, dossier_keys: list[str]) -> list[str]:
    """Qualify each persisted dossier version; return outcomes by key order."""
    outcomes: list[str] = []
    for key in dossier_keys:
        record = get_record_in_transaction(conn, run_id, KIND_DOSSIER_VERSION, key)
        if record is None:
            raise WorkflowError(f"missing dossier version for {key}")
        qualification = qualify_dossier(record.payload.get("dossier", {}))
        put_record_in_transaction(
            conn, run_id, kind=KIND_QUALIFICATION, record_key=key,
            version=1, payload=qualification, digest=digest_payload(qualification),
        )
        outcomes.append(str(qualification["outcome"]))
        transition = (
            "qualified" if qualification["outcome"] == "qualified" else "incomplete"
        )
        _record_pilot_stage(
            conn, run_id, key, dict(record.payload.get("dossier", {}) or {}),
            transition, str(record.payload.get("property_id", "") or ""),
        )
    return outcomes
