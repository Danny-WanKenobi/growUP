"""Readiness probe (verifies a real PostgreSQL connection)."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import get_database_config
from app.db import DatabaseNotConfiguredError, check_database_connection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["readiness"])


@router.get("/ready")
def get_readiness() -> JSONResponse:
    """Return 200 only when PostgreSQL answers ``SELECT 1``.

    Returns 503 with a generic category otherwise. Responses and logs
    never include credentials or driver error details.
    """
    cfg = get_database_config()
    if not cfg.is_configured:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "not_configured"},
        )
    try:
        connected = check_database_connection(cfg)
    except DatabaseNotConfiguredError:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "not_configured"},
        )
    except Exception as exc:
        logger.warning("database readiness check failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unreachable"},
        )
    if not connected:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unreachable"},
        )
    return JSONResponse(status_code=200, content={"status": "ok", "database": "connected"})
