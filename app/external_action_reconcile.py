"""Human-approved, evidence-gated reconciliation (Phase 3C-3B-3B).

STAGE 3B-3B-1 (this file, current): evidence contracts and pure
validation only. There is intentionally no ``record_reconciliation``
function, no database access, and no state transition in this
stage -- those arrive in 3B-3B-2. A test pins that absence.

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

REPLAY CONTRACT (implemented in 3B-3B-2; documented here as the
frozen interface that stage must honor):

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
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.identity import CLOCK_SKEW_SECONDS, canonical_evidence_digest

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
