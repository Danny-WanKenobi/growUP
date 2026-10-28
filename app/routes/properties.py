"""Property endpoints (MVP-002 shared data foundation)."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.auth import require_intake_key
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.properties import Property, PropertyCreate
from app.properties_db import create_property, get_property, list_properties

logger = logging.getLogger(__name__)

router = APIRouter(tags=["properties"])


@router.post("/properties", response_model=Property, status_code=status.HTTP_201_CREATED)
def create_property_endpoint(
    body: PropertyCreate,
    response: Response,
    _: None = Depends(require_intake_key),
) -> dict:
    """Create one sourced property record.

    Requires the X-Intake-Key header. Estimates require traceable
    provenance, verified fields require per-field evidence, and
    data_status is derived server-side. Unknown fields stay null;
    hypothetical values are rejected.
    """
    try:
        row = create_property(body.model_dump())
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_not_configured"
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("property create failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc
    response.headers["Location"] = f"/properties/{row['id']}"
    return row


@router.get("/properties", response_model=list[Property])
def list_properties_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: None = Depends(require_intake_key),
) -> list:
    """List stored properties, newest first.

    Requires the X-Intake-Key header.
    """
    try:
        return list_properties(limit=limit, offset=offset)
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_not_configured"
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("property list failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc


@router.get("/properties/{property_id}", response_model=Property)
def get_property_endpoint(
    property_id: UUID,
    _: None = Depends(require_intake_key),
) -> dict:
    """Return one property by id, or 404 when it does not exist.

    Requires the X-Intake-Key header.
    """
    try:
        row = get_property(property_id)
    except DatabaseNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_not_configured"
        ) from exc
    except DatabaseUnavailableError as exc:
        logger.warning("property fetch failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database_unavailable"
        ) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="property_not_found")
    return row
