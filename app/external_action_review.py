"""Human review presentation for dossiers (Phase 3C-3B-3B-3B-2).

Pure, read-only, dependency-free: transforms one dossier mapping
from get_reconciliation_case() into a structured human review
report. This module performs no database queries, no network
requests, no filesystem writes, no authorization, and no
execution -- and it imports nothing capable of those. The
dossier's authorization and snapshot guarantees remain entirely
the responsibility of the reader that produced it.

The presentation never broadens the supplied information: every
section copies an explicit allowlist of fields, coerces values
to JSON-compatible scalars (UUIDs and datetimes become
strings), and never mutates the input. Unknown fields are
dropped, never echoed, so unexpected nested content --
including anything sensitive a caller smuggled into the
mapping -- cannot leak into the report.

Honesty rules enforced throughout: a database record is not
proof of provider-side execution; a reported claim is not
independently verified evidence; an absent record is not proof
an event never happened; UNKNOWN stays the historical attempt
outcome after reconciliation; ATTEMPTING is never safe to
retry; and ready_for_human_review never authorizes
reconciliation. Malformed or self-contradictory dossiers raise
ReviewInputError rather than produce misleading guidance.

Evidence limitation, stated explicitly: the dossier carries no
signal that distinguishes a documented claim from a merely
reported one beyond the recorded review tier, so populated
reference fields alone never confer credibility. Recorded-but-
unreviewed evidence is conservatively classified `reported`
however complete its references; `documented` stays in the
approved vocabulary for a future dossier signal that can
actually establish it. Completeness is reported independently
and never implies verification.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID


class ReviewInputError(ValueError):
    """The supplied dossier is malformed or self-contradictory."""


_STATUS_STATES = {
    "unresolved_unknown": frozenset({"UNKNOWN"}),
    "attempting": frozenset({"ATTEMPTING"}),
    "stale_attempting": frozenset({"ATTEMPTING"}),
    "reconciled": frozenset({"SUCCEEDED", "FAILED"}),
    "terminal": frozenset({"SUCCEEDED", "FAILED"}),
    "authorized": frozenset({"AUTHORIZED"}),
    "pending": frozenset({"PENDING"}),
}

_REVIEW_REASONS = {
    "unresolved_unknown": (
        "Uncertain execution outcome awaits human review."
    ),
    "attempting": (
        "No recorded outcome yet; review awaits execution resolution."
    ),
    "stale_attempting": (
        "Long-running attempt may warrant investigation; elapsed"
        " time proves nothing."
    ),
    "reconciled": (
        "Human-reviewed resolution recorded; confirmatory review only."
    ),
    "terminal": (
        "Terminal outcome recorded directly; reconciliation review"
        " does not apply."
    ),
    "authorized": (
        "Execution not started; reconciliation review does not apply."
    ),
    "pending": (
        "Execution not started; reconciliation review does not apply."
    ),
}

_ATTEMPT_DESCRIPTIONS = {
    "open": (
        "Claimed by the recorded executor; no provider outcome"
        " recorded. Claimed is not confirmed executed."
    ),
    "succeeded": (
        "Success recorded in the ledger; a database record is not"
        " proof of provider-side execution."
    ),
    "failed": (
        "Failure recorded in the ledger; a database record is not"
        " proof of provider-side execution."
    ),
    "unknown": (
        "Execution ended uncertainly; UNKNOWN is preserved history,"
        " never rewritten by reconciliation."
    ),
}

_EVIDENCE_REFERENCE_FIELDS = (
    "kind",
    "source",
    "observed_at",
    "digest",
    "summary",
    "artifact_ref",
    "provider_reference",
    "storage_ref",
)

# Retrievable-reference core: all four present means a reviewer
# can locate the cited documentation. Provider/storage refs stay
# optional (nullable in the ledger); observed_at/summary describe.
_EVIDENCE_CORE_FIELDS = ("kind", "source", "artifact_ref", "digest")


def _as_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, UUID):
        return str(value)
    raise ReviewInputError(f"dossier field {name!r} must be text or null")


def _as_time(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, datetime):
        return value.isoformat()
    raise ReviewInputError(
        f"dossier field {name!r} must be a timestamp or null"
    )


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ReviewInputError(f"dossier field {name!r} must be a boolean")


def _as_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is int:
        return value
    raise ReviewInputError(
        f"dossier field {name!r} must be an integer or null"
    )


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ReviewInputError(f"dossier field {name!r} must be an object")


def _as_list(value: Any, name: str) -> list:
    if isinstance(value, list):
        return value
    raise ReviewInputError(f"dossier field {name!r} must be a list")


def _timeline_key(event: dict[str, Any]) -> tuple:
    raw = event["occurred_at"]
    stamp = None
    if isinstance(raw, str):
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            stamp = None
    if stamp is not None and stamp.tzinfo is not None:
        return (0, stamp.timestamp(), event["event_id"] or "")
    return (1, 0.0, event["event_id"] or "")  # unknown times sort last


def _build_attempts(
    entries: list, *, action_id: str, attempt_count: int
) -> list[dict[str, Any]]:
    history = []
    seen_ids: set[str] = set()
    previous_number: int | None = None
    for entry in entries:
        attempt = _as_mapping(entry, "attempts[]")
        attempt_id = _as_text(
            attempt.get("attempt_id"), "attempt.attempt_id"
        )
        if attempt_id is None:
            raise ReviewInputError("attempt.attempt_id is required")
        if attempt_id in seen_ids:
            raise ReviewInputError("duplicate attempt_id in dossier history")
        seen_ids.add(attempt_id)
        attempt_action = _as_text(
            attempt.get("action_id"), "attempt.action_id"
        )
        if attempt_action is None:
            raise ReviewInputError("attempt.action_id is required")
        if attempt_action != action_id:
            raise ReviewInputError(
                "attempt.action_id does not match the parent action"
            )
        number = _as_int(
            attempt.get("attempt_number"), "attempt.attempt_number"
        )
        if number is None:
            raise ReviewInputError("attempt.attempt_number is required")
        if number < 1:
            raise ReviewInputError("attempt numbers must be positive")
        if previous_number is not None and number <= previous_number:
            raise ReviewInputError(
                "attempt history is not ordered by attempt number"
            )
        previous_number = number
        if number > attempt_count:
            raise ReviewInputError(
                "attempt number exceeds action.attempt_count"
            )
        outcome = attempt.get("outcome")
        if outcome is None:
            category = "open"
        elif outcome in ("SUCCEEDED", "FAILED", "UNKNOWN"):
            category = outcome.lower()
        else:
            raise ReviewInputError(
                "attempt outcome outside the recorded vocabulary"
            )
        history.append({
            "attempt_id": attempt_id,
            "attempt_number": number,
            "executor_id": _as_text(
                attempt.get("executor_id"), "attempt.executor_id"
            ),
            "claimed_at": _as_time(
                attempt.get("claimed_at"), "attempt.claimed_at"
            ),
            "outcome": _as_text(outcome, "attempt.outcome"),
            "outcome_recorded_at": _as_time(
                attempt.get("outcome_recorded_at"),
                "attempt.outcome_recorded_at",
            ),
            "category": category,
            "description": _ATTEMPT_DESCRIPTIONS[category],
        })
    return history


def _check_decision_linkage(
    decision: Mapping[str, Any],
    history: list[dict[str, Any]],
    state: str,
    truncated: bool,
) -> bool:
    """Validate the decision against the displayed history.

    Returns True when the referenced attempt was found on the
    page and verified; False when truncation makes the linkage
    unverifiable (tolerated, but the caller must disclose it).
    """
    attempt_id = _as_text(
        decision.get("attempt_id"), "decision.attempt_id"
    )
    if attempt_id is None:
        raise ReviewInputError("decision.attempt_id is required")
    outcome = _as_text(decision.get("outcome"), "decision.outcome")
    if outcome is None:
        raise ReviewInputError("decision.outcome is required")
    if outcome != state:
        raise ReviewInputError(
            "reconciliation outcome does not match the terminal action state"
        )
    match = next(
        (a for a in history if a["attempt_id"] == attempt_id), None
    )
    if match is None:
        # A truncated page legitimately omits the resolved (latest)
        # attempt: unverifiable is not inconsistent. Only a complete
        # page with a dangling reference fails closed.
        if truncated:
            return False
        raise ReviewInputError(
            "reconciliation attempt_id matches no historical attempt"
        )
    if match["outcome"] != "UNKNOWN":
        raise ReviewInputError(
            "reconciled attempt does not preserve its UNKNOWN history"
        )
    return True


def _build_audits(entries: list) -> list[dict[str, Any]]:
    timeline = []
    for entry in entries:
        event = _as_mapping(entry, "audits[]")
        timeline.append({
            "event_id": _as_text(event.get("event_id"), "audit.event_id"),
            "occurred_at": _as_time(
                event.get("occurred_at"), "audit.occurred_at"
            ),
            "event_type": _as_text(
                event.get("event_type"), "audit.event_type"
            ),
            "severity": _as_text(event.get("severity"), "audit.severity"),
            "summary": _as_text(event.get("summary"), "audit.summary"),
        })
    timeline.sort(key=_timeline_key)
    return timeline


def _empty_evidence() -> dict[str, Any]:
    return {
        "classification": "missing",
        "verification_tier": None,
        "completeness": "missing",
        "decision_id": None,
        "outcome": None,
        "prior_outcome": None,
        "references": dict.fromkeys(_EVIDENCE_REFERENCE_FIELDS),
        "limitations": [
            "No reconciliation evidence is recorded for this action."
        ],
    }


def _build_evidence(
    decision: Mapping[str, Any] | None, *, linkage_verified: bool = True
) -> dict[str, Any]:
    if decision is None:
        return _empty_evidence()
    references = {
        "kind": _as_text(decision.get("evidence_kind"), "evidence.kind"),
        "source": _as_text(
            decision.get("evidence_source"), "evidence.source"
        ),
        "observed_at": _as_time(
            decision.get("evidence_observed_at"), "evidence.observed_at"
        ),
        "digest": _as_text(decision.get("evidence_digest"), "evidence.digest"),
        "summary": _as_text(
            decision.get("evidence_summary"), "evidence.summary"
        ),
        "artifact_ref": _as_text(
            decision.get("artifact_ref"), "evidence.artifact_ref"
        ),
        "provider_reference": _as_text(
            decision.get("provider_reference"),
            "evidence.provider_reference",
        ),
        "storage_ref": _as_text(
            decision.get("storage_ref"), "evidence.storage_ref"
        ),
    }
    tier = _as_text(decision.get("verification_tier"), "evidence.tier")
    core = [references[name] is not None for name in _EVIDENCE_CORE_FIELDS]
    any_present = any(value is not None for value in references.values())
    # Credibility comes only from the recorded review tier, never
    # from populated fields: a complete but unverified provider
    # claim is reported, not documented (see module docstring).
    if tier == "human_reviewed":
        classification = "human_reviewed"
    elif any_present:
        classification = "reported"
    else:
        classification = "missing"
    if all(core):
        completeness = "complete"
    elif any_present:
        completeness = "partial"
    else:
        completeness = "missing"
    limitations = [
        "Referenced artifacts are cited, not independently inspected"
        " or authenticated by this report.",
        "A reported provider claim is not independently verified evidence.",
    ]
    if not linkage_verified:
        limitations.append(
            "The reconciliation decision is recorded, but its relationship"
            " to the resolved attempt cannot be confirmed from the truncated"
            " history displayed in this report."
        )
    if tier == "human_reviewed":
        limitations.append(
            "human_reviewed means a human reviewed the evidence; it is"
            " not independent provider verification by Grow Up."
        )
    elif tier is not None:
        limitations.append(
            "Unrecognized verification tier recorded; treated as unreviewed."
        )
    if completeness != "complete":
        absent = sorted(
            name for name, present in zip(_EVIDENCE_CORE_FIELDS, core)
            if not present
        )
        limitations.append(
            "Evidence references incomplete; absent: "
            + ", ".join(absent)
            + "."
        )
    return {
        "classification": classification,
        "verification_tier": tier,
        "completeness": completeness,
        "decision_id": _as_text(
            decision.get("decision_id"), "decision.decision_id"
        ),
        "outcome": _as_text(decision.get("outcome"), "decision.outcome"),
        "prior_outcome": _as_text(
            decision.get("prior_outcome"), "decision.prior_outcome"
        ),
        "references": references,
        "limitations": limitations,
    }


def _build_guidance(
    status: str,
    history: list[dict[str, Any]],
    attempts_truncated: bool,
    audits_truncated: bool,
) -> dict[str, Any]:
    missing: list[str] = []
    steps: list[str] = []
    if status == "unresolved_unknown":
        numbered = [a for a in history if a["attempt_number"] is not None]
        latest = (
            max(numbered, key=lambda a: a["attempt_number"])
            if numbered
            else None
        )
        if latest is not None and latest["attempt_id"] is not None:
            steps.append(
                "Identify the provider operation behind attempt "
                f"{latest['attempt_id']}."
            )
        else:
            steps.append(
                "Identify the provider operation behind the latest attempt."
            )
        steps.extend([
            "Obtain the provider's receipt or status reference through"
            " established channels.",
            "Confirm any evidence observation predates the approval"
            " that cites it.",
            "Record findings; any decision requires a separate signed"
            " human approval.",
        ])
        missing.append(
            "Provider receipt or status reference for the UNKNOWN outcome."
        )
    elif status in ("attempting", "stale_attempting"):
        missing.append("Recorded execution outcome.")
        if status == "stale_attempting":
            steps.append(
                "Check provider-side status through established channels"
                " if available."
            )
            steps.append(
                "Treat elapsed time as a prompt to look, never as a verdict."
            )
        else:
            steps.append(
                "Await execution resolution; no review action is available."
            )
            steps.append("Do not intervene in a running attempt.")
    elif status == "reconciled":
        steps.append(
            "Confirm the recorded decision matches the provider documentation."
        )
        steps.append(
            "No further review action is required unless documentation"
            " conflicts."
        )
    elif status == "terminal":
        steps.append(
            "No reconciliation review applies; the outcome was recorded"
            " directly."
        )
    else:  # authorized, pending
        steps.append(
            "No reconciliation review applies; execution has not started."
        )
    if attempts_truncated:
        missing.append("Complete attempt history (display truncated).")
    if audits_truncated:
        missing.append("Complete audit history (display truncated).")
    return {
        "missing_information": missing,
        "permissible_steps": steps,
        "prohibitions": [
            "Do not retry, requeue, or re-execute this action.",
            "Do not treat staleness or elapsed time as failure or as"
            " permission to act.",
            "Do not present a database record as proof of provider-side"
            " execution.",
            "Reconciliation requires a separate, signed human approval;"
            " this report authorizes nothing.",
        ],
    }


def _build_readiness(
    status: str,
    decided: bool,
    attempts_truncated: bool,
    audits_truncated: bool,
) -> dict[str, Any]:
    if decided:
        return {
            "status": "review_completed",
            "reason": (
                "A reconciliation decision is recorded; confirmatory"
                " review only."
            ),
        }
    if status == "unresolved_unknown":
        if attempts_truncated or audits_truncated:
            return {
                "status": "needs_evidence",
                "reason": (
                    "Case history is incomplete; full context is needed first."
                ),
            }
        return {
            "status": "ready_for_human_review",
            "reason": (
                "Uncertain outcome with complete case history; ready for"
                " human investigation. This status does not authorize"
                " reconciliation."
            ),
        }
    if status in ("attempting", "stale_attempting"):
        return {
            "status": "needs_evidence",
            "reason": (
                "No recorded outcome yet; review awaits execution resolution."
            ),
        }
    if status == "terminal":
        return {
            "status": "not_applicable",
            "reason": (
                "Terminal outcome recorded directly; no UNKNOWN outcome"
                " to reconcile."
            ),
        }
    return {
        "status": "not_applicable",
        "reason": (
            "Execution has not started; reconciliation review does not apply."
        ),
    }


def build_reconciliation_review(dossier: Mapping[str, Any]) -> dict[str, Any]:
    """Build a human review report from one reconciliation case dossier.

    Pure presentation: reads the supplied mapping, validates its
    shape and self-consistency, and returns fresh JSON-compatible
    structures. Raises ReviewInputError for malformed or
    contradictory input. Never mutates the dossier, never
    queries, and never authorizes anything.
    """
    if not isinstance(dossier, Mapping):
        raise ReviewInputError("dossier must be a mapping")
    action = _as_mapping(dossier.get("action"), "action")
    state = _as_text(action.get("state"), "action.state")
    if state is None:
        raise ReviewInputError("dossier action.state is required")
    action_id = _as_text(action.get("action_id"), "action.action_id")
    if action_id is None:
        raise ReviewInputError("dossier action.action_id is required")
    attempt_count = _as_int(action.get("attempt_count"), "action.attempt_count")
    if attempt_count is None or attempt_count < 0:
        raise ReviewInputError(
            "dossier action.attempt_count must be a non-negative integer"
        )
    interpretation = _as_mapping(
        dossier.get("interpretation"), "interpretation"
    )
    status = _as_text(interpretation.get("status"), "interpretation.status")
    if status not in _STATUS_STATES:
        raise ReviewInputError(f"unknown interpretation status: {status!r}")
    if state not in _STATUS_STATES[status]:
        raise ReviewInputError(
            "action.state contradicts interpretation.status"
        )
    attempts_raw = _as_list(dossier.get("attempts"), "attempts")
    audits_raw = _as_list(dossier.get("audits"), "audits")
    attempts_truncated = _as_bool(
        dossier.get("attempts_truncated"), "attempts_truncated"
    )
    audits_truncated = _as_bool(
        dossier.get("audits_truncated"), "audits_truncated"
    )
    raw_decision = dossier.get("reconciliation")
    if raw_decision is not None and not isinstance(raw_decision, Mapping):
        raise ReviewInputError(
            "dossier reconciliation must be an object or null"
        )
    if (raw_decision is not None) != (status == "reconciled"):
        raise ReviewInputError(
            "reconciliation presence contradicts interpretation.status"
        )
    history = _build_attempts(
        attempts_raw, action_id=action_id, attempt_count=attempt_count
    )
    linkage_verified = True
    if raw_decision is not None:
        linkage_verified = _check_decision_linkage(
            raw_decision, history, state, attempts_truncated
        )
    timeline = _build_audits(audits_raw)
    evidence = _build_evidence(
        raw_decision, linkage_verified=linkage_verified
    )
    guidance = _build_guidance(
        status, history, attempts_truncated, audits_truncated
    )
    readiness = _build_readiness(
        status,
        raw_decision is not None,
        attempts_truncated,
        audits_truncated,
    )
    history_note = None
    if attempts_truncated:
        history_note = (
            f"Attempt history is truncated to the first {len(history)};"
            " the full history was not reviewed."
        )
    elif not history:
        history_note = "No execution attempts recorded."
    timeline_note = None
    if audits_truncated:
        timeline_note = (
            "Audit history is truncated; the displayed timeline is incomplete."
        )
    elif not timeline:
        timeline_note = (
            "No permanent lifecycle audits recorded for this action."
        )
    return {
        "case_summary": {
            "action_id": _as_text(action.get("action_id"), "action.action_id"),
            "idempotency_key": _as_text(
                action.get("idempotency_key"), "action.idempotency_key"
            ),
            "agent": _as_text(action.get("agent"), "action.agent"),
            "action_type": _as_text(
                action.get("action_type"), "action.action_type"
            ),
            "state": state,
            "interpretation_status": status,
            "interpretation_detail": _as_text(
                interpretation.get("detail"), "interpretation.detail"
            ),
            "review_reason": _REVIEW_REASONS[status],
        },
        "authorization": {
            "authorized_by": _as_text(
                action.get("authorized_by"), "action.authorized_by"
            ),
            "authorization_reference": _as_text(
                action.get("authorization_reference"),
                "action.authorization_reference",
            ),
            "authorized_at": _as_time(
                action.get("authorized_at"), "action.authorized_at"
            ),
        },
        "execution_history": {
            "attempts": history,
            "attempts_truncated": attempts_truncated,
            "note": history_note,
        },
        "evidence_assessment": evidence,
        "audit_timeline": {
            "events": timeline,
            "truncated": audits_truncated,
            "note": timeline_note,
        },
        "review_guidance": guidance,
        "readiness": readiness,
    }
