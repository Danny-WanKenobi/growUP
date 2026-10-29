"""Identity-review inspection models (IR-3B, read-only).

Pydantic contracts for the identity-review list/detail endpoints.
Research-specific fields stay nullable so future IR-3C rows flow
through these same contracts without a second breaking change.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel


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
