"""Signed human identity-review resolutions (IR-4A).

``record_review_resolution`` is the single atomic transaction
resolving one PENDING identity-review case to RESOLVED with its
immutable decision row and permanent audit event. It mirrors
``app.external_action_reconcile.record_reconciliation``: approvals
are purpose-bound Ed25519 signatures (``growup-review-v1:`` domain,
separate reviewer enrollment), evidence freshness is bound into the
signature via the expected occurrence count, and terminal means
terminal (identical replays return the committed decision; anything
else already decided fails closed).

What this module never does: merge, delete, or modify property
records or listing evidence; touch RESOLVED history; or accept
reconciliation-domain signatures or enrollment. No HTTP route
references this module (guarded by tests/test_identity.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity import (
    REVIEW_RESOLUTION_OUTCOMES,
    AuthenticationFailedError,
    AuthorizationDeniedError,
    canonical_review_digest,
    ensure_may_resolve_review,
    verify_review_approval,
)
from app.observability.events import (
    AgentEvent,
    AgentId,
    EvidenceRef,
    RetentionClass,
    Severity,
)
from app.observability.store import insert_event_in_transaction
from app.properties_db import get_connection

REVIEW_RESOLUTION_EVENT_TYPE = "identity.review.resolved"
PRIOR_STATUS_PENDING = "PENDING"
RESOLVED_STATUS = "RESOLVED"

MAX_EVIDENCE_SUMMARY_CHARS = 500

_DECISION_NAMESPACE = uuid5(NAMESPACE_URL, "growup-review-decision-v1")

_DECISION_COLUMNS = (
    "decision_id",
    "review_id",
    "outcome",
    "prior_status",
    "resolved_by",
    "resolution_reference",
    "approval_signature",
    "signing_public_key",
    "signing_key_fingerprint",
    "approval_issued_at",
    "approval_expires_at",
    "evidence_digest",
    "evidence_snapshot",
    "expected_occurrence_count",
    "evidence_summary",
)


class ReviewNotFoundError(RuntimeError):
    """No identity-review case exists for the supplied id."""


class StaleReviewError(ValueError):
    """The approval's expected occurrence count no longer matches the case.

    New evidence landed after the approval was minted; the reviewer
    must re-examine the case and sign a fresh approval. Raised
    before any write.
    """


class ReviewResolutionConflictError(RuntimeError):
    """A different decision already resolves this review.

    Raised when a resubmission's outcome, digest, principal, or
    reference differs from the committed decision: terminal means
    terminal, and new evidence cannot amend a recorded decision.
    """


@dataclass(frozen=True)
class ResolutionResult:
    """Outcome of one resolution request (created vs replayed)."""

    decision: dict[str, Any]
    review: dict[str, Any]
    created: bool
    audit_event_id: str | None


def _coerce_uuid(value: UUID | str, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def _clean_summary(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("evidence_summary must be a string")
    cleaned = value.strip()
    if len(cleaned) > MAX_EVIDENCE_SUMMARY_CHARS:
        raise ValueError("evidence_summary exceeds 500 characters")
    return cleaned


def canonical_review_snapshot(
    *,
    review_id: UUID | str,
    outcome: str,
    expected_occurrence_count: int,
    summary: object,
) -> dict[str, Any]:
    """Build the validated evidence snapshot the approval vouches for.

    Shared by operator tooling (via the digest) and the writer: the
    server rebuilds this snapshot from the submission and digests it
    before any cryptography runs, so tampering fails closed.
    """
    rid = _coerce_uuid(review_id, "review_id")
    if outcome not in REVIEW_RESOLUTION_OUTCOMES:
        raise ValueError(f"invalid proposed_outcome: {outcome!r}")
    if (
        isinstance(expected_occurrence_count, bool)
        or not isinstance(expected_occurrence_count, int)
        or expected_occurrence_count < 1
    ):
        raise ValueError("expected_occurrence_count must be a positive integer")
    return {
        "review_id": str(rid),
        "outcome": outcome,
        "expected_occurrence_count": expected_occurrence_count,
        "summary": _clean_summary(summary),
    }


def _quiet_rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass  # original failure already decided the outcome


def _quiet_close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass  # cleanup must not mask the real outcome


def _resolution_audit(review: dict[str, Any], decision: dict[str, Any]) -> AgentEvent:
    """Build the permanent resolution audit (no secrets, no evidence bodies)."""
    return AgentEvent(
        agent=AgentId.GABRIEL,
        run_id=str(review["review_id"]),
        event_type=REVIEW_RESOLUTION_EVENT_TYPE,
        summary=(
            f"Human-resolved identity review {review['review_id']} "
            f"PENDING -> RESOLVED ({decision['outcome']}) "
            f"on reviewed evidence (decision {decision['decision_id']})"
        ),
        severity=Severity.INFO,
        retention_class=RetentionClass.PERMANENT,
        details={
            "review_id": str(review["review_id"]),
            "decision_id": str(decision["decision_id"]),
            "from_status": PRIOR_STATUS_PENDING,
            "to_status": RESOLVED_STATUS,
            "outcome": decision["outcome"],
            "resolved_by": decision["resolved_by"],
            "resolution_reference": decision["resolution_reference"],
            "evidence_digest": decision["evidence_digest"],
            "expected_occurrence_count": decision["expected_occurrence_count"],
            "verification_tier": "human_reviewed",
        },
        evidence_refs=[
            EvidenceRef(
                kind="review_resolution_evidence", ref=decision["evidence_digest"]
            )
        ],
        correlation_id=str(review.get("run_id") or ""),
        property_id=None,
    )


def _replay_existing(
    conn: Any,
    review: dict[str, Any],
    winner: dict[str, Any],
    *,
    proposed_outcome: str,
    digest: str,
    reviewer: str,
    resolution_reference: str,
    expected_occurrence_count: int,
) -> ResolutionResult:
    """Return a committed decision without writing, or fail closed.

    The submission must match the winner on outcome, digest,
    principal, reference, and expected count, and the case must
    already sit in RESOLVED. Anything else is a conflict or a
    state this code could never have produced.
    """
    if (
        winner["outcome"] != proposed_outcome
        or winner["evidence_digest"] != digest
        or winner["resolved_by"] != reviewer
        or winner["resolution_reference"] != resolution_reference
        or int(winner["expected_occurrence_count"]) != expected_occurrence_count
    ):
        raise ReviewResolutionConflictError(
            "a different decision already resolves this review"
        )
    if review["status"] != RESOLVED_STATUS:
        raise DatabaseUnavailableError("review_resolution_state_mismatch")
    _quiet_rollback(conn)
    return ResolutionResult(
        decision=dict(winner),
        review=dict(review),
        created=False,
        audit_event_id=None,
    )


def record_review_resolution(
    *,
    review_id: UUID | str,
    proposed_outcome: str,
    evidence_summary: object = "",
    expected_occurrence_count: int,
    resolved_by: str,
    resolution_reference: str,
    approval_signature: str,
    approval_issued_at: datetime,
    approval_expires_at: datetime,
    config: DatabaseConfig | None = None,
) -> ResolutionResult:
    """Resolve one PENDING review to RESOLVED, atomically audited.

    Two phases. Before any connection opens: identifier shape,
    snapshot canonicalization and digest binding, and Ed25519
    approval verification against reviewer enrollment. Inside one
    PostgreSQL transaction: lock the review row, confirm it is
    PENDING with the approved occurrence count, then either replay
    an existing decision (writing nothing) or insert the decision,
    flip the case to RESOLVED, and insert the permanent audit
    event, committing all three together. Any failure rolls
    everything back.

    Replay: an identical resubmission (same review, outcome,
    digest, principal, reference, count -- a renewed valid
    approval may carry fresh timestamps) returns the committed
    decision and writes nothing. Anything else already decided
    raises :class:`ReviewResolutionConflictError`.

    This function performs no property, evidence, or occurrence
    writes: resolutions change queue state only.
    """
    rid = _coerce_uuid(review_id, "review_id")
    snapshot = canonical_review_snapshot(
        review_id=rid,
        outcome=proposed_outcome,
        expected_occurrence_count=expected_occurrence_count,
        summary=evidence_summary,
    )
    digest = canonical_review_digest(snapshot)
    approved = verify_review_approval(
        claimed_name=resolved_by,
        review_id=rid,
        proposed_outcome=proposed_outcome,
        evidence_digest=digest,
        expected_occurrence_count=expected_occurrence_count,
        resolution_reference=resolution_reference,
        issued_at=approval_issued_at,
        expires_at=approval_expires_at,
        signature_hex=approval_signature,
    )
    ensure_may_resolve_review(approved.principal)
    reviewer = approved.principal.name
    decision_id = uuid5(_DECISION_NAMESPACE, str(rid))
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM identity_reviews WHERE review_id = %(review_id)s"
                " FOR UPDATE",
                {"review_id": rid},
            )
            review = cur.fetchone()
        if review is None:
            raise ReviewNotFoundError(f"unknown review_id: {rid}")
        review = dict(review)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM identity_review_decisions"
                " WHERE review_id = %(review_id)s",
                {"review_id": rid},
            )
            existing = cur.fetchone()
        if existing is not None:
            return _replay_existing(
                conn,
                review,
                dict(existing),
                proposed_outcome=proposed_outcome,
                digest=digest,
                reviewer=reviewer,
                resolution_reference=resolution_reference,
                expected_occurrence_count=expected_occurrence_count,
            )
        # First-time path: the case must validate as PENDING with the
        # approved evidence count before any decision row is inserted.
        if review["status"] != PRIOR_STATUS_PENDING:
            raise ValueError(
                f"review status {review['status']} is not resolvable (must be PENDING)"
            )
        if int(review["occurrence_count"]) != expected_occurrence_count:
            raise StaleReviewError(
                "review evidence changed since approval: "
                f"expected count {expected_occurrence_count}, "
                f"found {review['occurrence_count']}"
            )
        columns = ", ".join(_DECISION_COLUMNS)
        placeholders = ", ".join(f"%({name})s" for name in _DECISION_COLUMNS)
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO identity_review_decisions ({columns})"
                f" VALUES ({placeholders})"
                " ON CONFLICT (review_id) DO NOTHING RETURNING *",
                {
                    "decision_id": decision_id,
                    "review_id": rid,
                    "outcome": proposed_outcome,
                    "prior_status": PRIOR_STATUS_PENDING,
                    "resolved_by": reviewer,
                    "resolution_reference": resolution_reference,
                    "approval_signature": approval_signature,
                    "signing_public_key": approved.public_key_hex,
                    "signing_key_fingerprint": approved.key_fingerprint,
                    "approval_issued_at": approval_issued_at,
                    "approval_expires_at": approval_expires_at,
                    "evidence_digest": digest,
                    "evidence_snapshot": json.dumps(snapshot, sort_keys=True),
                    "expected_occurrence_count": expected_occurrence_count,
                    "evidence_summary": snapshot["summary"],
                },
            )
            inserted = cur.fetchone()
        if inserted is None:
            # Unreachable while every resolver takes the review row
            # lock first (a rival's decision commits before we proceed
            # and lands us on the replay path above), but fail closed
            # rather than assume: re-read and replay-or-conflict.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM identity_review_decisions"
                    " WHERE review_id = %(review_id)s",
                    {"review_id": rid},
                )
                winner = cur.fetchone()
            if winner is None:
                raise DatabaseUnavailableError("review_resolution_race")
            return _replay_existing(
                conn,
                review,
                dict(winner),
                proposed_outcome=proposed_outcome,
                digest=digest,
                reviewer=reviewer,
                resolution_reference=resolution_reference,
                expected_occurrence_count=expected_occurrence_count,
            )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE identity_reviews SET status = %(status)s, updated_at = now()"
                " WHERE review_id = %(review_id)s RETURNING *",
                {"status": RESOLVED_STATUS, "review_id": rid},
            )
            updated_review = cur.fetchone()
        decision = dict(inserted)
        audit = _resolution_audit(dict(updated_review), decision)
        insert_event_in_transaction(conn, audit)
        conn.commit()
        return ResolutionResult(
            decision=decision,
            review=dict(updated_review),
            created=True,
            audit_event_id=audit.event_id,
        )
    except (
        ReviewNotFoundError,
        StaleReviewError,
        ReviewResolutionConflictError,
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
        ValueError,
        TypeError,
    ):
        _quiet_rollback(conn)
        raise
    except Exception as exc:
        _quiet_rollback(conn)
        raise DatabaseUnavailableError("review_resolution_failed") from exc
    finally:
        _quiet_close(conn)
