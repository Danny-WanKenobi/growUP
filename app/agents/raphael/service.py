"""Raphael authorization service (deterministic, inspectable).

Loads a persisted, server-owned dossier version and decides whether it
carries sufficient evidence for a narrowly scoped preliminary
*simulation*:

- authorized: the exact dossier version meets every gate below.
- needs_evidence: explicit unmet requirements (fail-closed success).
- rejected: evidence actively disqualifies the claim (documented).

A category decision never establishes structural safety, zoning
permission, tenant relocation rights, agricultural feasibility, or
profitability; those limits are recorded on every decision.

Gates for `authorized` (all required):
1. The dossier version's stored digest matches its recomputed digest.
2. The persisted candidate verification says VERIFIED with a pathway
   and no failure reasons.
3. No identity disputes on the dossier.
4. No unresolved conflicts on the dossier.
5. Category attribution is not contradictory.

`rejected` applies only when the dossier's own evidence contradicts
the category claim. Anything else missing yields `needs_evidence`.

Decisions are idempotent per dossier digest: re-authorizing the same
exact version returns the stored decision.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.agents.raphael import RAPHAEL_POLICY_VERSION
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties_db import get_connection
from app.workflow import KIND_RAPHAEL_DECISION, digest_payload, is_demo_id
from app.workflow_db import (
    WorkflowError,
    get_record_in_transaction,
    put_record_in_transaction,
)

SCOPE = (
    "Preliminary simulation scoping only: the dossier's category claim "
    "is supported well enough to model a hypothetical indoor farm. This "
    "decision authorizes engineering estimates, not execution."
)
LIMITATIONS = [
    "Does not establish structural safety or load capacity.",
    "Does not establish zoning permission or code compliance.",
    "Does not establish tenant relocation rights or lease outcomes.",
    "Does not establish agricultural feasibility or profitability.",
]


def _dossier_digest(payload: dict[str, Any]) -> str:
    return digest_payload({k: v for k, v in payload.items() if k != "digest"})


def _verification_blockers(verification: Any) -> list[str]:
    if not isinstance(verification, dict):
        return ["no persisted category verification on the dossier version"]
    if verification.get("outcome") != "VERIFIED":
        reasons = verification.get("reasons") or ["unexplained"]
        return [f"category verification {verification.get('outcome')}: {', '.join(map(str, reasons))}"]
    if not verification.get("pathway"):
        return ["verified category lacks a verification pathway"]
    if verification.get("reasons"):
        return [f"verified category carries failure reasons: {verification.get('reasons')}"]
    return []


def evaluate_dossier(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure gate evaluation over a persisted dossier-version payload."""
    dossier = payload.get("dossier", {}) if isinstance(payload, dict) else {}
    basis: list[str] = []
    blockers = _verification_blockers(payload.get("candidate_verification"))
    basis.extend(blockers)
    disputes = dossier.get("identity_disputes") or {}
    if disputes:
        basis.append(f"unresolved identity disputes: {sorted(disputes)}")
    conflicts = dossier.get("conflicts") or []
    if conflicts:
        basis.append(f"{len(conflicts)} unresolved dossier conflict(s)")
    category = dossier.get("category_attribution") or {}
    contradictory = str(category.get("status", "")).lower() == "contradictory"
    if contradictory:
        values = category.get("conflicting_values") or []
        return {
            "outcome": "rejected",
            "basis": basis + [f"category evidence contradicts itself: {values}"],
        }
    if blockers or disputes or conflicts:
        return {"outcome": "needs_evidence", "basis": basis}
    return {"outcome": "authorized", "basis": ["all authorization gates satisfied"]}


def authorize_in_transaction(
    conn: Any, run_id: UUID, dossier_key: str, version: int | None = None
) -> dict[str, Any]:
    """Authorize one persisted dossier version; idempotent per digest."""
    if is_demo_id(dossier_key):
        raise WorkflowError("demo identifiers are rejected by production authorization")
    record = get_record_in_transaction(conn, run_id, "dossier_version", dossier_key, version)
    if record is None:
        raise WorkflowError(f"no dossier version for {dossier_key}")
    if not record.digest or _dossier_digest(record.payload) != record.digest:
        raise WorkflowError(f"dossier version {dossier_key} failed digest binding")
    verdict = evaluate_dossier(record.payload)
    decision = {
        "outcome": verdict["outcome"],
        "dossier_key": dossier_key,
        "dossier_digest": record.digest,
        "dossier_version": record.version,
        "basis": verdict["basis"],
        "policy_version": RAPHAEL_POLICY_VERSION,
        "scope": SCOPE,
        "limitations": list(LIMITATIONS),
    }
    stored = put_record_in_transaction(
        conn, run_id, kind=KIND_RAPHAEL_DECISION, record_key=record.digest,
        version=1, payload=decision, digest=digest_payload(decision),
    )
    # Pilot stage counters reflect actual assessment: every decision is
    # an assessment; authorized/rejected record their terminal stage
    # while needs_evidence records an incomplete one. Unknown cells or
    # unresolved properties record nothing. Keyed by the canonical
    # operational dossier identity (cross-run dedup-safe).
    from app.pilot import (
        PilotError,
        canonical_quota_id,
        cell_from_dossier,
        record_transition_in_transaction,
    )

    cell = cell_from_dossier(dict(record.payload.get("dossier", {}) or {}))
    if cell is not None:
        try:
            quota_id = canonical_quota_id(
                str(record.payload.get("property_id", "") or "")
            )
        except PilotError:
            quota_id = None
        if quota_id is not None:
            county, category = cell
            record_transition_in_transaction(
                conn, county=county, category=category, dossier_id=quota_id,
                transition="assessed", run_id=run_id, record_key=dossier_key,
            )
            record_transition_in_transaction(
                conn, county=county, category=category, dossier_id=quota_id,
                transition={
                    "authorized": "authorized",
                    "rejected": "rejected",
                }.get(str(verdict["outcome"]), "incomplete"),
                run_id=run_id, record_key=dossier_key,
            )
    return stored.payload


def authorize_dossier(
    run_id: UUID, dossier_key: str, version: int | None = None, config: Any = None
) -> dict[str, Any]:
    """Own a connection and authorize one dossier version (commit once).

    Note: the permanent audit event for this business decision is
    written by the stage wrapper through the consequential coordinator;
    this helper persists the decision row only.
    """
    conn = get_connection(config)
    try:
        decision = authorize_in_transaction(conn, run_id, dossier_key, version)
        conn.commit()
        return decision
    except (WorkflowError, DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
