"""Reconciliation review endpoint (authenticated, strictly read-only).

One endpoint: GET /external-actions/{action_id}/review returns the
sanitized report from build_reconciliation_review() for the dossier
assembled by get_reconciliation_case(). The route verifies
orchestrator credentials, maps failures to generic HTTP statuses,
and adds Cache-Control: no-store to every response it produces.
It performs no authorization logic of its own (scopes live in the
reader), never returns the raw dossier, and cannot mutate,
execute, retry, or reconcile anything.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.external_action_reads import get_reconciliation_case
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
