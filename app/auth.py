"""Minimal API-key authentication (intake and research only).

Each protected endpoint requires its own shared secret header, compared
with ``hmac.compare_digest`` against an environment variable. Missing
configuration fails closed (503); missing or wrong credentials return
401. Keys never appear in logs or error responses.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status

INTAKE_KEY_ENV_VAR = "INTAKE_API_KEY"
INTAKE_KEY_HEADER = "X-Intake-Key"
RESEARCH_KEY_ENV_VAR = "RESEARCH_API_KEY"
RESEARCH_KEY_HEADER = "X-Research-Key"


def _require_key(request: Request, env_var: str, header: str, config_detail: str) -> None:
    expected = os.getenv(env_var, "")
    if not expected or not expected.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=config_detail,
        )
    provided = request.headers.get(header)
    if not provided or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )
    return None


def require_intake_key(request: Request) -> None:
    """FastAPI dependency enforcing the intake API key."""
    _require_key(request, INTAKE_KEY_ENV_VAR, INTAKE_KEY_HEADER, "intake_not_configured")


def require_research_key(request: Request) -> None:
    """FastAPI dependency enforcing the research API key."""
    _require_key(request, RESEARCH_KEY_ENV_VAR, RESEARCH_KEY_HEADER, "research_not_configured")
