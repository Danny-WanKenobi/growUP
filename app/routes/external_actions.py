"""Reconciliation review endpoints (authenticated, strictly read-only).

GET /external-actions/review-queue lists UNKNOWN actions awaiting
human review, oldest first, for authorized orchestrators.
GET /external-actions/{action_id}/review returns the sanitized
report from build_reconciliation_review() for one dossier. Both
routes verify orchestrator credentials, map failures to generic
HTTP statuses, and add Cache-Control: no-store to every response
they produce. They perform no authorization logic of their own
(scopes live in the readers), never return raw rows or dossiers,
and cannot mutate, execute, retry, or reconcile anything.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.external_action_reads import (
    get_reconciliation_case,
    list_unknown_actions,
)
from app.external_action_review import (
    ReviewInputError,
    build_reconciliation_review,
)
from app.identity import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    verify_orchestrator,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["external-actions"])

ORCHESTRATOR_ID_HEADER = "X-Orchestrator-ID"
ORCHESTRATOR_PROOF_HEADER = "X-Orchestrator-Proof"

_NO_STORE = {"Cache-Control": "no-store"}


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="unauthorized",
        headers=_NO_STORE,
    )


def _not_found() -> HTTPException:
    # Nonexistent and unauthorized actions share one response:
    # callers must not distinguish them.
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="review_not_found",
        headers=_NO_STORE,
    )


def _present_queue_case(record: dict) -> dict:
    """Project one UNKNOWN-action row onto the public field allowlist.

    Only the five contracted fields survive; idempotency keys,
    authorization references, executor ids, and attempt details
    never leave the server. Records with a missing, non-datetime,
    or timezone-naive timestamp, an unusable UTC offset, or a
    non-UNKNOWN state raise DatabaseUnavailableError so the queue
    fails closed with 503 instead of serving a suspect case. Valid
    timestamps are normalized to UTC with the Z suffix, preserving
    fractional seconds.
    """
    if record.get("state") != "UNKNOWN":
        raise DatabaseUnavailableError("queue_record_invalid")
    observed = record.get("outcome_recorded_at")
    if (
        not isinstance(observed, datetime)
        or observed.tzinfo is None
        or observed.utcoffset() is None
    ):
        raise DatabaseUnavailableError("queue_record_invalid")
    try:
        normalized = observed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError) as exc:
        raise DatabaseUnavailableError("queue_record_invalid") from exc
    if normalized.endswith("+00:00"):
        normalized = normalized[:-6] + "Z"
    return {
        "action_id": str(record["action_id"]),
        "action_type": record["action_type"],
        "state": record["state"],
        "attempt_count": record["attempt_count"],
        "awaiting_review_since": normalized,
    }


@router.get("/external-actions/review-queue")
def get_review_queue(
    request: Request,
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Return UNKNOWN actions awaiting human review, oldest first.

    Requires the X-Orchestrator-ID and X-Orchestrator-Proof headers.
    Scope enforcement happens inside list_unknown_actions(); an
    authorized caller with no visible cases receives an empty 200.
    """
    response.headers["Cache-Control"] = "no-store"
    orchestrator_id = request.headers.get(ORCHESTRATOR_ID_HEADER)
    proof = request.headers.get(ORCHESTRATOR_PROOF_HEADER)
    if (
        not orchestrator_id
        or not orchestrator_id.strip()
        or not proof
        or not proof.strip()
    ):
        raise _unauthorized()
    try:
        verify_orchestrator(orchestrator_id=orchestrator_id, proof=proof)
    except AuthenticationFailedError as exc:
        logger.warning("review queue request denied: %s", type(exc).__name__)
        raise _unauthorized() from exc
    try:
        records = list_unknown_actions(
            orchestrator_id=orchestrator_id,
            orchestrator_proof=proof,
            limit=limit,
        )
        # Presentation runs inside the guarded block: an invalid
        # record raises DatabaseUnavailableError and the whole
        # request fails closed with 503 instead of serving a
        # partial or suspect queue.
        cases = [_present_queue_case(record) for record in records]
    except AuthenticationFailedError as exc:
        logger.warning("review queue request denied: %s", type(exc).__name__)
        raise _unauthorized() from exc
    except AuthorizationDeniedError as exc:
        logger.warning("review queue request denied: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden",
            headers=_NO_STORE,
        ) from exc
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_not_configured",
            headers=_NO_STORE,
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("review queue request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_unavailable",
            headers=_NO_STORE,
        ) from exc
    return {
        "cases": cases,
        "count": len(cases),
        "limit": limit,
    }


@router.get("/external-actions/{action_id}/review")
def get_review(
    action_id: UUID, request: Request, response: Response
) -> dict:
    """Return the sanitized reconciliation review report for one action.

    Requires the X-Orchestrator-ID and X-Orchestrator-Proof headers.
    Scope enforcement happens inside get_reconciliation_case(); this
    route only maps its None (missing or unauthorized alike) to 404.
    """
    response.headers["Cache-Control"] = "no-store"
    orchestrator_id = request.headers.get(ORCHESTRATOR_ID_HEADER)
    proof = request.headers.get(ORCHESTRATOR_PROOF_HEADER)
    if (
        not orchestrator_id
        or not orchestrator_id.strip()
        or not proof
        or not proof.strip()
    ):
        raise _unauthorized()
    try:
        verify_orchestrator(orchestrator_id=orchestrator_id, proof=proof)
    except AuthenticationFailedError as exc:
        logger.warning("review request denied: %s", type(exc).__name__)
        raise _unauthorized() from exc
    try:
        dossier = get_reconciliation_case(
            action_id=action_id,
            orchestrator_id=orchestrator_id,
            orchestrator_proof=proof,
        )
    except AuthenticationFailedError as exc:
        logger.warning("review request denied: %s", type(exc).__name__)
        raise _unauthorized() from exc
    except AuthorizationDeniedError as exc:
        logger.warning("review request denied: %s", type(exc).__name__)
        raise _not_found() from exc
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_not_configured",
            headers=_NO_STORE,
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("review request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database_unavailable",
            headers=_NO_STORE,
        ) from exc
    if dossier is None:
        raise _not_found()
    try:
        return build_reconciliation_review(dossier)
    except ReviewInputError as exc:
        logger.warning("review request failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="review_conflict",
            headers=_NO_STORE,
        ) from exc
