"""Minimal API-key authentication (intake, research, and Wondrue).

Each protected endpoint requires its own shared secret header, compared
with ``hmac.compare_digest`` against an environment variable. Missing
configuration fails closed (503); missing or wrong credentials return
401. Keys never appear in logs or error responses.

Wondrue uses two enrolled credentials on one header: the operator key
grants the full mode range, the reader key grants read-only. The grant
is derived server-side from the credential; a caller-supplied mode
never confers authority beyond the grant.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status

INTAKE_KEY_ENV_VAR = "INTAKE_API_KEY"
INTAKE_KEY_HEADER = "X-Intake-Key"
RESEARCH_KEY_ENV_VAR = "RESEARCH_API_KEY"
RESEARCH_KEY_HEADER = "X-Research-Key"
WONDRUE_KEY_HEADER = "X-Wondrue-Key"
WONDRUE_OPERATOR_KEY_ENV_VAR = "WONDRUE_OPERATOR_KEY"
WONDRUE_READER_KEY_ENV_VAR = "WONDRUE_READER_KEY"


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


def _matches(provided: str, expected: str) -> bool:
    if not expected or not expected.strip():
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def wondrue_grant(request: Request) -> str:
    """Authenticate a Wondrue caller; return the server-side mode grant.

    Returns ``"full"`` for the operator credential, ``"read-only"`` for
    the reader credential. Unconfigured credentials fail closed (503);
    missing or unknown credentials return 401.
    """
    operator = os.getenv(WONDRUE_OPERATOR_KEY_ENV_VAR, "")
    reader = os.getenv(WONDRUE_READER_KEY_ENV_VAR, "")
    if (not operator or not operator.strip()) and (not reader or not reader.strip()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="wondrue_not_configured",
        )
    provided = request.headers.get(WONDRUE_KEY_HEADER)
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
        )
    if _matches(provided, operator):
        return "full"
    if _matches(provided, reader):
        return "read-only"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized"
    )
