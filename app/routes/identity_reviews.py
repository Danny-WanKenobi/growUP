"""Identity-review inspection and resolution endpoints (IR-3B/IR-4B).

GET /identity-reviews lists durable review cases with optional
exact-match filters and stable newest-activity-first ordering.
GET /identity-reviews/{review_id} returns one case with its bounded
occurrence history and decision-event summary.
POST /identity-reviews/{review_id}/resolve records one signed human
resolution through the atomic writer. All routes require the
X-Intake-Key header, map database failures to generic 503s, and add
Cache-Control: no-store to every response they produce. Only the
resolve endpoint mutates, and only via record_review_resolution:
one decision row, one status flip, one audit event -- never
properties or evidence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.auth import require_intake_key
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity_reviews import (
    IdentityReviewDetail,
    IdentityReviewList,
    IdentityReviewResolutionRequest,
    IdentityReviewResolutionResult,
    IdentityReviewSummary,
)
from app.identity_review_resolve import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    ReviewNotFoundError,
    ReviewResolutionConflictError,
    StaleReviewError,
    record_review_resolution,
)
from app.identity_reviews_db import (
    REVIEW_LIST_DEFAULT_LIMIT,
    REVIEW_LIST_MAX_LIMIT,
    get_identity_review,
    get_review_summary,
    list_identity_reviews,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity-reviews"])

_NO_STORE = {"Cache-Control": "no-store"}

# Longest creation window a summary may cover: a 365-day span can
# hold at most 366 UTC calendar days, which structurally bounds the
# daily resolution series without any silent truncation.
_SUMMARY_MAX_WINDOW_DAYS = 365
_SUMMARY_DEFAULT_WINDOW_DAYS = 365


@router.get("/identity-reviews", response_model=IdentityReviewList)
def list_reviews_endpoint(
    response: Response,
    status_filter: Literal["PENDING", "RESOLVED"] | None = Query(
        default=None, alias="status"
    ),
    source: Literal["intake", "discovery", "research"] | None = Query(default=None),
    reason_code: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=REVIEW_LIST_DEFAULT_LIMIT, ge=1, le=REVIEW_LIST_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(require_intake_key),
) -> dict:
    """List durable identity-review cases, newest activity first.

    Requires the X-Intake-Key header. All filters are optional
    exact matches; the total uses the same filters as the page.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        reviews, total = list_identity_reviews(
            status=status_filter,
            source=source,
            reason_code=reason_code,
            limit=limit,
            offset=offset,
        )
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_not_configured",
            headers=_NO_STORE,
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("review list failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_unavailable",
            headers=_NO_STORE,
        ) from exc
    return {"reviews": reviews, "total": total, "limit": limit, "offset": offset}


@router.get("/identity-reviews/summary", response_model=IdentityReviewSummary)
def get_review_summary_endpoint(
    response: Response,
    source: Literal["intake", "discovery", "research"] | None = Query(default=None),
    run_id: str | None = Query(default=None, max_length=256),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    _: None = Depends(require_intake_key),
) -> dict:
    """Return one read-only aggregate snapshot of the review queue.

    Registered before /{review_id} so "summary" never parses as a
    UUID. All filters select the same review set using
    identity_reviews.created_at; since and until must be aware,
    ordered, and at most 365 days apart. Without explicit bounds
    the trailing 365-day window is served, always disclosed via
    filters_echo. resolutions_by_day additionally restricts its
    daily series to decisions whose decided_at falls within the
    effective window, so decisions.total may exceed the sum of
    resolutions_by_day.
    """
    response.headers["Cache-Control"] = "no-store"
    for bound in (since, until):
        if bound is not None and bound.tzinfo is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="invalid_range",
                headers=_NO_STORE,
            )
    now = datetime.now(timezone.utc)
    effective_until = until if until is not None else now
    if since is not None:
        effective_since = since
    else:
        effective_since = effective_until - timedelta(days=_SUMMARY_DEFAULT_WINDOW_DAYS)
    if effective_until <= effective_since or (
        effective_until - effective_since
    ).total_seconds() > _SUMMARY_MAX_WINDOW_DAYS * 86400:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_range",
            headers=_NO_STORE,
        )
    try:
        aggregates = get_review_summary(
            source=source,
            run_id=run_id,
            since=effective_since,
            until=effective_until,
        )
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_not_configured",
            headers=_NO_STORE,
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("review summary failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_unavailable",
            headers=_NO_STORE,
        ) from exc
    return {
        "generated_at": now,
        "filters_echo": {
            "source": source,
            "run_id": run_id,
            "since": since,
            "until": until,
            "effective_since": effective_since,
            "effective_until": effective_until,
        },
        **aggregates,
    }


@router.get("/identity-reviews/{review_id}", response_model=IdentityReviewDetail)
def get_review_endpoint(
    review_id: UUID,
    response: Response,
    _: None = Depends(require_intake_key),
) -> dict:
    """Return one identity-review case with history and provenance.

    Requires the X-Intake-Key header. Unknown review IDs return 404;
    malformed UUIDs are rejected with 422 before any database access.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        detail = get_identity_review(review_id)
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_not_configured",
            headers=_NO_STORE,
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("review fetch failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_unavailable",
            headers=_NO_STORE,
        ) from exc
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="review_not_found",
            headers=_NO_STORE,
        )
    return detail


@router.post(
    "/identity-reviews/{review_id}/resolve",
    response_model=IdentityReviewResolutionResult,
    status_code=status.HTTP_201_CREATED,
)
def resolve_review_endpoint(
    review_id: UUID,
    body: IdentityReviewResolutionRequest,
    response: Response,
    _: None = Depends(require_intake_key),
) -> dict:
    """Record one signed human resolution for a review case.

    Requires the X-Intake-Key header; the URL supplies the review
    id and the body carries the signed approval. New decisions
    return 201; identical replays return the committed decision
    with 200 and write nothing. No verification, enrollment, or
    SQL lives here: everything delegates to the atomic writer.
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        result = record_review_resolution(
            review_id=review_id,
            proposed_outcome=body.proposed_outcome,
            evidence_summary=body.evidence_summary,
            expected_occurrence_count=body.expected_occurrence_count,
            resolved_by=body.resolved_by,
            resolution_reference=body.resolution_reference,
            approval_signature=body.approval_signature,
            approval_issued_at=body.approval_issued_at,
            approval_expires_at=body.approval_expires_at,
        )
    except AuthenticationFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_approval",
            headers=_NO_STORE,
        ) from exc
    except AuthorizationDeniedError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden",
            headers=_NO_STORE,
        ) from exc
    except ReviewNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="review_not_found",
            headers=_NO_STORE,
        ) from exc
    except ReviewResolutionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="resolution_conflict",
            headers=_NO_STORE,
        ) from exc
    except StaleReviewError as exc:
        # A ValueError subclass: must precede the generic validation arm.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="stale_evidence",
            headers=_NO_STORE,
        ) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="invalid_resolution",
            headers=_NO_STORE,
        ) from exc
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_not_configured",
            headers=_NO_STORE,
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("review resolve failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_unavailable",
            headers=_NO_STORE,
        ) from exc
    if not result.created:
        response.status_code = status.HTTP_200_OK
    decision = result.decision
    review = result.review
    return {
        "review_id": review["review_id"],
        "decision_id": decision["decision_id"],
        "outcome": decision["outcome"],
        "status": review["status"],
        "created": result.created,
        "audit_event_id": result.audit_event_id,
        "occurrence_count": review["occurrence_count"],
        "resolved_by": decision["resolved_by"],
        "resolution_reference": decision["resolution_reference"],
        "evidence_digest": decision["evidence_digest"],
    }
