"""Identity-review inspection models (IR-3B, read-only).

Pydantic contracts for the identity-review list/detail endpoints.
Research-specific fields stay nullable so future IR-3C rows flow
through these same contracts without a second breaking change.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IdentityReviewOccurrence(BaseModel):
    """One counted encounter linked to its permanent decision event."""

    occurrence_id: UUID
    event_id: str
    created_at: datetime


class DecisionEventSummary(BaseModel):
    """Bounded provenance for the review's originating decision event.

    Carries linkage and classification only; the complete event-details
    payload stays on the observability query surface.
    """

    event_id: str
    event_type: str
    outcome: str | None = None
    reason_code: str | None = None
    occurred_at: datetime


class IdentityReview(BaseModel):
    """One durable identity-review case as persisted."""

    review_id: UUID
    dedup_key: str
    source: str
    reason_code: str
    observed_identity: dict[str, Any]
    provider: str | None = None
    external_id: str | None = None
    listing_url: str | None = None
    conflicting_property_ids: list[UUID] = []
    candidate_key: str | None = None
    identity_disputes: dict[str, Any] | None = None
    decision_event_id: str
    run_id: str
    status: str
    occurrence_count: int
    last_event_id: str
    supersedes_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class IdentityReviewDetail(IdentityReview):
    """One case plus its bounded occurrence history and provenance."""

    occurrences: list[IdentityReviewOccurrence] = []
    occurrences_truncated: bool = False
    decision_event: DecisionEventSummary


class IdentityReviewList(BaseModel):
    """One paginated review-listing page with its total."""

    reviews: list[IdentityReview] = []
    total: int
    limit: int
    offset: int


class IdentityReviewResolutionRequest(BaseModel):
    """One signed human-resolution submission (IR-4B).

    Shape validation only: the URL supplies the review id (never
    duplicated here), and the writer re-validates every field plus
    the Ed25519 approval before any database access. Unknown
    fields are rejected so smuggled parameters fail closed.
    """

    model_config = ConfigDict(extra="forbid")

    proposed_outcome: Literal["SAME_PROPERTY", "DISTINCT_PROPERTIES"]
    expected_occurrence_count: int = Field(ge=1)
    resolved_by: str = Field(max_length=64)
    resolution_reference: str = Field(min_length=1, max_length=256)
    approval_signature: str = Field(pattern=r"^[0-9a-f]{128}$")
    approval_issued_at: datetime
    approval_expires_at: datetime
    evidence_summary: str = Field(default="", max_length=500)

    @field_validator("approval_issued_at", "approval_expires_at")
    @classmethod
    def _require_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("must be a timezone-aware datetime")
        return value


class IdentityReviewResolutionResult(BaseModel):
    """One recorded (or replayed) resolution decision.

    Carries linkage and classification only: signatures, public
    keys, and authentication secrets never leave the server.
    """

    review_id: UUID
    decision_id: UUID
    outcome: str
    status: str
    created: bool
    audit_event_id: str | None = None
    occurrence_count: int
    resolved_by: str
    resolution_reference: str
    evidence_digest: str


class SummaryCases(BaseModel):
    """Case counts over the filtered review set."""

    total: int
    pending: int
    resolved: int


class SummaryOccurrences(BaseModel):
    """Occurrence-row counts over the filtered review set."""

    total: int
    unresolved: int


class DecisionsByOutcome(BaseModel):
    """Signed decisions grouped by recorded outcome."""

    SAME_PROPERTY: int
    DISTINCT_PROPERTIES: int


class SummaryDecisions(BaseModel):
    """Signed human decisions over the filtered review set."""

    total: int
    by_outcome: DecisionsByOutcome


class CasesBySource(BaseModel):
    """Filtered cases grouped by originating pipeline."""

    intake: int
    discovery: int
    research: int


class OccurrenceBuckets(BaseModel):
    """Filtered cases grouped by their occurrence-row count."""

    one: int
    two_to_three: int
    four_or_more: int


class ResolutionDay(BaseModel):
    """One UTC calendar day with its resolution count."""

    day: date
    count: int


class SummaryFiltersEcho(BaseModel):
    """Supplied filters plus the effective creation window served."""

    source: str | None = None
    run_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    effective_since: datetime
    effective_until: datetime


class IdentityReviewSummary(BaseModel):
    """One read-only aggregate snapshot of the review queue.

    Every count derives from committed review, occurrence, and
    decision rows in a single database snapshot. Empty sets
    report zeros, never nulls; approval signatures, keys, and
    secrets can never appear here.
    """

    generated_at: datetime
    filters_echo: SummaryFiltersEcho
    cases: SummaryCases
    occurrences: SummaryOccurrences
    decisions: SummaryDecisions
    cases_by_source: CasesBySource
    repeated_case_count: int
    occurrence_buckets: OccurrenceBuckets
    resolutions_by_day: list[ResolutionDay]
