"""Minimal API-key authentication (MVP-004A intake only).

Protects ``POST /discovery/intake`` with a shared secret: the client
sends ``X-Intake-Key`` and the server compares it against the
``INTAKE_API_KEY`` environment variable using ``hmac.compare_digest``.
Missing configuration fails closed (503); missing or wrong credentials
return 401. Keys never appear in logs or error responses.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status

INTAKE_KEY_ENV_VAR = "INTAKE_API_KEY"
INTAKE_KEY_HEADER = "X-Intake-Key"


def require_intake_key(request: Request) -> None:
    """FastAPI dependency enforcing the intake API key."""
    expected = os.getenv(INTAKE_KEY_ENV_VAR, "")
    if not expected or not expected.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="intake_not_configured",
        )
    provided = request.headers.get(INTAKE_KEY_HEADER)
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )
    return None
