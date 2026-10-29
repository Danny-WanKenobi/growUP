"""Identity-review inspection endpoints (IR-3B, strictly read-only).

GET /identity-reviews lists durable review cases with optional
exact-match filters and stable newest-activity-first ordering.
GET /identity-reviews/{review_id} returns one case with its bounded
occurrence history and decision-event summary. Both routes require
the X-Intake-Key header, map database failures to generic 503s, and
add Cache-Control: no-store to every response they produce. They
cannot create, resolve, mutate, or delete anything.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.auth import require_intake_key
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity_reviews import IdentityReviewDetail, IdentityReviewList
from app.identity_reviews_db import (
    REVIEW_LIST_DEFAULT_LIMIT,
    REVIEW_LIST_MAX_LIMIT,
    get_identity_review,
    list_identity_reviews,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["identity-reviews"])

_NO_STORE = {"Cache-Control": "no-store"}


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
