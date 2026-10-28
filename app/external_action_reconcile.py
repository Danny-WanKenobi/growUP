"""Human-approved, evidence-gated reconciliation (Phase 3C-3B-3B).

STAGE 3B-3B-2 (this file, current): adds ``record_reconciliation``,
the single atomic PostgreSQL transaction resolving one UNKNOWN
action to SUCCEEDED or FAILED with its decision row and permanent
audit. No execution, retry, requeue, or network behavior exists
here or anywhere in the lifecycle: recovering a decision never
authorizes repeating external work (GRW-EXT-001).

What this module establishes:

- ``ReconciliationEvidence``: the immutable, validated shape of
  one human-reviewed evidence item (provider receipt or provider
  status reference). Unknown fields are rejected so the signed
  bytes can never silently differ from the validated model.
- Canonical digest binding: the human signs the digest of the
  exact normalized evidence; the server recomputes and compares
  before any cryptography runs, so tampering fails closed.
- Timing and consistency guards: evidence must predate its
  approval (documented skew), must not come from the future, and
  must assert exactly the proposed outcome.

Honesty contract (read carefully): Grow Up authenticates the
HUMAN's approval and protects evidence INTEGRITY. It does not
independently establish provider truth -- no provider API, status
lookup, or signed receipt verification exists in this codebase.
Every decision row therefore carries ``human_reviewed`` as its
verification tier, and audit language must say "human-reconciled
on reviewed documentation," never "verified."

REPLAY CONTRACT (implemented below; frozen since 3B-3B-1):

- One definitive reconciliation decision per attempt, enforced
  by UNIQUE(attempt_id).
- An identical replay -- same attempt, principal, reference,
  outcome, and evidence digest -- returns the existing decision
  and writes nothing: no new decision, audit event, or state
  transition.
- A renewed approval by the SAME principal and reference (fresh
  timestamps over identical evidence) discovers the existing
  decision after acknowledgment loss without modifying it.
- A different principal or reference is NOT an identical replay
  and fails closed with a conflict; conflicting outcomes or
  digests likewise fail closed.

HISTORY CONTRACT: reconciliation never rewrites the attempt it
resolves. The attempt row keeps outcome UNKNOWN -- the true
record that execution ended uncertainly -- while the decision
row carries the subsequently reviewed outcome and only the
parent action flips to the terminal state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.external_actions_db import ActionNotFoundError, StaleAttemptError
from app.identity import (
    CLOCK_SKEW_SECONDS,
    AuthenticationFailedError,
    AuthorizationDeniedError,
    canonical_evidence_digest,
    ensure_may_reconcile,
    verify_reconciliation_approval,
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

EVIDENCE_KINDS = ("provider_receipt", "provider_status_reference")
RECONCILABLE_OUTCOMES = ("SUCCEEDED", "FAILED")

MAX_EVIDENCE_SOURCE_CHARS = 200
MAX_EVIDENCE_SUMMARY_CHARS = 500
MAX_EVIDENCE_REF_CHARS = 500


class InsufficientEvidenceError(ValueError):
    """Submitted evidence cannot substantiate the proposed outcome.

    A ValueError subclass so generic shape-error handlers keep
    working, while callers that care can catch the specific case.
    Raised for missing, incompatible, contradictory, mistimed, or
    otherwise inadequate evidence -- always before any write.
    """


class ReconciliationConflictError(RuntimeError):
    """A different decision already resolves this attempt (3B-3B-2).

    Raised when a replay's principal, reference, outcome, or digest
    differs from the committed decision: terminal means terminal,
    and new evidence cannot amend a recorded decision.
    """


def _parse_observed_at(value: object) -> datetime:
    """Parse one ISO-8601 observation timestamp to canonical UTC."""
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("observed_at must be an ISO-8601 string") from None
    if parsed.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _require_aware_moment(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


class ReconciliationEvidence(BaseModel):
    """One validated human-reviewed evidence item (immutable).

    ``observed_at`` is stored in canonical UTC ISO form so every
    spelling of the same instant digests identically. Whitespace
    padding is stripped (documented operator contract); empty or
    whitespace-only required fields are rejected; unknown fields
    are rejected so signed bytes match validated bytes exactly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["provider_receipt", "provider_status_reference"]
    source: str = Field(min_length=1, max_length=MAX_EVIDENCE_SOURCE_CHARS)
    observed_at: str
    asserts_outcome: Literal["SUCCEEDED", "FAILED"]
    artifact_ref: str = Field(min_length=1, max_length=MAX_EVIDENCE_REF_CHARS)
    provider_reference: str | None = Field(default=None, max_length=MAX_EVIDENCE_REF_CHARS)
    storage_ref: str | None = Field(default=None, max_length=MAX_EVIDENCE_REF_CHARS)
    summary: str = Field(default="", max_length=MAX_EVIDENCE_SUMMARY_CHARS)

    @field_validator("observed_at")
    @classmethod
    def _normalize_observed_at(cls, value: object) -> str:
        return _parse_observed_at(value).isoformat()

    @field_validator("source", "artifact_ref")
    @classmethod
    def _require_nonblank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be a non-empty string")
        return cleaned

    @field_validator("summary")
    @classmethod
    def _strip_summary(cls, value: str) -> str:
        return value.strip()

    @field_validator("provider_reference", "storage_ref")
    @classmethod
    def _clean_optional_ref(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string or null")
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be non-empty when provided")
        return cleaned

    def digest(self) -> str:
        """Canonical digest of this evidence (what the human signs)."""
        return canonical_evidence_digest(self.model_dump(mode="json"))

    def as_row(self) -> dict[str, Any]:
        """Normalized evidence fields for the decision row (3B-3B-2 input)."""
        return self.model_dump(mode="json")


def ensure_outcome_consistent(
    evidence: ReconciliationEvidence, proposed_outcome: str
) -> None:
    """Evidence must assert exactly the proposed outcome (no drift)."""
    if proposed_outcome not in RECONCILABLE_OUTCOMES:
        raise ValueError(f"invalid proposed_outcome: {proposed_outcome!r}")
    if evidence.asserts_outcome != proposed_outcome:
        raise InsufficientEvidenceError(
            "evidence does not support the proposed outcome"
        )


def ensure_observation_not_future(
    observed_at: str, *, now: datetime | None = None
) -> None:
    """Reject observations from the future beyond clock skew."""
    moment = _require_aware_moment(
        datetime.now(timezone.utc) if now is None else now, "now"
    )
    observed = _parse_observed_at(observed_at)
    if observed > moment + timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise InsufficientEvidenceError("evidence observed_at lies in the future")


def ensure_observation_predates_approval(
    observed_at: str, issued_at: datetime
) -> None:
    """Evidence must predate the approval vouching for it (plus skew).

    A human cannot vouch for evidence observed after signing; the
    small skew allowance covers signer/verifier clock difference,
    not backfilling.
    """
    issued = _require_aware_moment(issued_at, "issued_at")
    observed = _parse_observed_at(observed_at)
    if observed > issued + timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise InsufficientEvidenceError("evidence observed after approval issuance")


_DECISION_NAMESPACE = uuid5(NAMESPACE_URL, "growup-reconcile-v1")

_DECISION_COLUMNS = (
    "decision_id",
    "action_id",
    "attempt_id",
    "outcome",
    "asserts_outcome",
    "prior_outcome",
    "reconciled_by",
    "reconciliation_reference",
    "approval_signature",
    "signing_public_key",
    "signing_key_fingerprint",
    "approval_issued_at",
    "approval_expires_at",
    "evidence_kind",
    "evidence_source",
    "evidence_observed_at",
    "evidence_digest",
    "evidence_summary",
    "artifact_ref",
    "provider_reference",
    "storage_ref",
    "verification_tier",
)


@dataclass(frozen=True)
class ReconciliationResult:
    """Outcome of one reconciliation request (created vs replayed)."""

    decision: dict[str, Any]
    action: dict[str, Any]
    attempt: dict[str, Any]
    created: bool


def _coerce_uuid(value: UUID | str, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


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


def _reconciliation_audit(
    action: dict[str, Any], decision: dict[str, Any]
) -> AgentEvent:
    """Build the permanent reconciliation audit (no secrets, no evidence bodies)."""
    return AgentEvent(
        agent=AgentId(action["agent"]),
        run_id=str(action["action_id"]),
        event_type="external_action.reconciled",
        summary=(
            f"Human-reconciled action {action['action_id']} attempt "
            f"{decision['attempt_id']} UNKNOWN -> {decision['outcome']} "
            f"on reviewed documentation (decision {decision['decision_id']})"
        ),
        severity=Severity.INFO,
        retention_class=RetentionClass.PERMANENT,
        details={
            "action_id": str(action["action_id"]),
            "attempt_id": str(decision["attempt_id"]),
            "decision_id": str(decision["decision_id"]),
            "from_state": "UNKNOWN",
            "to_state": decision["outcome"],
            "reconciled_by": decision["reconciled_by"],
            "reconciliation_reference": decision["reconciliation_reference"],
            "evidence_digest": decision["evidence_digest"],
            "evidence_kind": decision["evidence_kind"],
            "verification_tier": "human_reviewed",
        },
        evidence_refs=[
            EvidenceRef(
                kind="reconciliation_evidence", ref=decision["evidence_digest"]
            )
        ],
        correlation_id=action["idempotency_key"],
        property_id=action["property_id"],
    )


def _replay_existing(
    conn: Any,
    action: dict[str, Any],
    attempt: dict[str, Any],
    winner: dict[str, Any],
    *,
    aid: UUID,
    proposed_outcome: str,
    digest: str,
    reconciler: str,
    reconciliation_reference: str,
) -> ReconciliationResult:
    """Return a committed decision without writing, or fail closed.

    The submission must match the winner on action, outcome,
    digest, principal, and reference; the action must already sit
    in the decision's terminal state; and the attempt must still
    carry its historical UNKNOWN outcome. Anything else is a
    conflict or a state this code could never have produced.
    """
    if (
        winner["action_id"] != aid
        or winner["outcome"] != proposed_outcome
        or winner["evidence_digest"] != digest
        or winner["reconciled_by"] != reconciler
        or winner["reconciliation_reference"] != reconciliation_reference
    ):
        raise ReconciliationConflictError(
            "a different decision already resolves this attempt"
        )
    if action["state"] != winner["outcome"] or attempt["outcome"] != "UNKNOWN":
        raise DatabaseUnavailableError("reconciliation_state_mismatch")
    _quiet_rollback(conn)
    return ReconciliationResult(
        decision=dict(winner),
        action=dict(action),
        attempt=dict(attempt),
        created=False,
    )


def record_reconciliation(
    *,
    action_id: UUID | str,
    attempt_id: UUID | str,
    proposed_outcome: str,
    evidence: ReconciliationEvidence | dict[str, Any],
    reconciled_by: str,
    reconciliation_reference: str,
    approval_signature: str,
    approval_issued_at: datetime,
    approval_expires_at: datetime,
    config: DatabaseConfig | None = None,
) -> ReconciliationResult:
    """Resolve one UNKNOWN action to SUCCEEDED/FAILED, atomically audited.

    Two phases. Before any connection opens: identifier shape,
    evidence parsing and outcome consistency, canonical digest
    binding, observation timing, and Ed25519 approval verification
    against reconciler enrollment. Inside one PostgreSQL
    transaction: lock the action and attempt rows, confirm the
    attempt belongs and is latest, then either replay an existing
    decision (writing nothing) or -- only after the action and
    attempt both validate as UNKNOWN -- insert the decision,
    flip the action to the definitive outcome, and insert the
    permanent audit event, committing all three together. Any
    failure rolls everything back. The attempt row is never
    written: its UNKNOWN outcome is the preserved history of an
    uncertain execution.

    Replay: an identical resubmission (same attempt, principal,
    reference, outcome, digest -- a renewed valid approval may
    carry fresh timestamps) returns the committed decision and
    writes nothing: no new decision, audit, or transition.
    Anything else already decided raises
    :class:`ReconciliationConflictError`. Terminal means terminal.

    This function performs no execution, retry, or requeue, and
    resolves UNKNOWN actions only: ATTEMPTING, SUCCEEDED, and
    FAILED actions are rejected, and the deferred generic
    transitions stay deferred.
    """
    aid = _coerce_uuid(action_id, "action_id")
    tid = _coerce_uuid(attempt_id, "attempt_id")
    if isinstance(evidence, ReconciliationEvidence):
        model = evidence
    elif isinstance(evidence, dict):
        model = ReconciliationEvidence(**evidence)
    else:
        raise TypeError("evidence must be a ReconciliationEvidence or dict")
    ensure_outcome_consistent(model, proposed_outcome)
    digest = model.digest()
    ensure_observation_not_future(model.observed_at)
    ensure_observation_predates_approval(model.observed_at, approval_issued_at)
    approved = verify_reconciliation_approval(
        claimed_name=reconciled_by,
        action_id=aid,
        attempt_id=tid,
        proposed_outcome=proposed_outcome,
        evidence_digest=digest,
        reconciliation_reference=reconciliation_reference,
        issued_at=approval_issued_at,
        expires_at=approval_expires_at,
        signature_hex=approval_signature,
    )
    ensure_may_reconcile(approved.principal)
    reconciler = approved.principal.name
    decision_id = uuid5(_DECISION_NAMESPACE, str(tid))
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM external_actions WHERE action_id = %(action_id)s"
                " FOR UPDATE",
                {"action_id": aid},
            )
            action = cur.fetchone()
        if action is None:
            raise ActionNotFoundError(f"unknown action_id: {aid}")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM external_action_attempts WHERE attempt_id = %(attempt_id)s"
                " FOR UPDATE",
                {"attempt_id": tid},
            )
            attempt = cur.fetchone()
        if attempt is None:
            raise StaleAttemptError("unknown attempt_id for reconciliation")
        if attempt["action_id"] != aid:
            raise StaleAttemptError("attempt belongs to a different action")
        if attempt["attempt_number"] != action["attempt_count"]:
            raise StaleAttemptError("attempt superseded by a newer attempt")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM reconciliation_decisions"
                " WHERE attempt_id = %(attempt_id)s",
                {"attempt_id": tid},
            )
            existing = cur.fetchone()
        if existing is not None:
            return _replay_existing(
                conn,
                action,
                attempt,
                dict(existing),
                aid=aid,
                proposed_outcome=proposed_outcome,
                digest=digest,
                reconciler=reconciler,
                reconciliation_reference=reconciliation_reference,
            )
        # First-time path: the action and attempt must both validate
        # as UNKNOWN before any decision row is inserted.
        if action["state"] != "UNKNOWN":
            raise ValueError(
                f"action state {action['state']} is not reconcilable (must be UNKNOWN)"
            )
        if attempt["outcome"] != "UNKNOWN":
            raise StaleAttemptError("attempt outcome is not UNKNOWN")
        columns = ", ".join(_DECISION_COLUMNS)
        placeholders = ", ".join(f"%({name})s" for name in _DECISION_COLUMNS)
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO reconciliation_decisions ({columns})"
                f" VALUES ({placeholders})"
                " ON CONFLICT (attempt_id) DO NOTHING RETURNING *",
                {
                    "decision_id": decision_id,
                    "action_id": aid,
                    "attempt_id": tid,
                    "outcome": proposed_outcome,
                    "asserts_outcome": model.asserts_outcome,
                    "prior_outcome": "UNKNOWN",
                    "reconciled_by": reconciler,
                    "reconciliation_reference": reconciliation_reference,
                    "approval_signature": approval_signature,
                    "signing_public_key": approved.public_key_hex,
                    "signing_key_fingerprint": approved.key_fingerprint,
                    "approval_issued_at": approval_issued_at,
                    "approval_expires_at": approval_expires_at,
                    "evidence_kind": model.kind,
                    "evidence_source": model.source,
                    "evidence_observed_at": datetime.fromisoformat(model.observed_at),
                    "evidence_digest": digest,
                    "evidence_summary": model.summary,
                    "artifact_ref": model.artifact_ref,
                    "provider_reference": model.provider_reference,
                    "storage_ref": model.storage_ref,
                    "verification_tier": "human_reviewed",
                },
            )
            inserted = cur.fetchone()
        if inserted is None:
            # Unreachable while every reconciler takes the action row
            # lock first (a rival's decision commits before we proceed
            # and lands us on the replay path above), but fail closed
            # rather than assume: re-read and replay-or-conflict.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM reconciliation_decisions"
                    " WHERE attempt_id = %(attempt_id)s",
                    {"attempt_id": tid},
                )
                winner = cur.fetchone()
            if winner is None:
                raise DatabaseUnavailableError("reconciliation_race")
            return _replay_existing(
                conn,
                action,
                attempt,
                dict(winner),
                aid=aid,
                proposed_outcome=proposed_outcome,
                digest=digest,
                reconciler=reconciler,
                reconciliation_reference=reconciliation_reference,
            )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE external_actions SET state = %(state)s, updated_at = now()"
                " WHERE action_id = %(action_id)s RETURNING *",
                {"state": proposed_outcome, "action_id": aid},
            )
            updated_action = cur.fetchone()
        decision = dict(inserted)
        audit = _reconciliation_audit(dict(action), decision)
        insert_event_in_transaction(conn, audit)
        conn.commit()
        return ReconciliationResult(
            decision=decision,
            action=dict(updated_action),
            attempt=dict(attempt),
            created=True,
        )
    except (
        ActionNotFoundError,
        StaleAttemptError,
        ReconciliationConflictError,
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
        raise DatabaseUnavailableError("reconciliation_failed") from exc
    finally:
        _quiet_close(conn)
