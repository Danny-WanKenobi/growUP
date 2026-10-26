"""Liveness probe (no database access)."""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__
from app.config import APP_NAME

router = APIRouter(tags=["health"])


@router.get("/health")
def get_health() -> dict:
    """Return HTTP 200 when the application process is alive."""
    return {"status": "ok", "service": APP_NAME, "version": __version__}
