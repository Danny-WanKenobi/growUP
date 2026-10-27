"""Centralized secret redaction and text sanitization (MVP-004C-A).

Every event passes through :func:`redact_event` before reaching any
sink, so agents cannot forget to scrub secrets. Matching is by field
name (recursive, case-insensitive substring) plus a small set of
high-confidence secret value patterns. Unknown structures are traversed,
never executed or interpreted.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***REDACTED***"

# Field-name fragments whose values are always redacted (case-insensitive).
_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "privatekey",
    "session",
    "cookie",
    "set-cookie",
    "x-subscription-token",
    "x-intake-key",
    "x-research-key",
)

# High-confidence secret shapes redacted even under innocent key names.
_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEYS)


def _scrub_value(text: str) -> str:
    scrubbed = text
    for pattern in _VALUE_PATTERNS:
        scrubbed = pattern.sub(REDACTED, scrubbed)
    return scrubbed


def scrub_text(text: str) -> str:
    """Remove secret value patterns from free text (summaries, refs)."""
    return _scrub_value(text)


def redact(obj: Any) -> Any:
    """Recursively redact secrets from JSON-compatible structures."""
    if isinstance(obj, dict):
        return {
            key: (REDACTED if _key_is_sensitive(str(key)) else redact(value))
            for key, value in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact(item) for item in obj]
    if isinstance(obj, str):
        return _scrub_value(obj)
    return obj


def sanitize_text(text: str) -> str:
    """Remove control characters that could corrupt log records or PG TEXT.

    Newlines are preserved here (JSON encoding keeps JSONL single-line);
    summaries are collapsed separately by the emitter for readability.
    """
    return _CONTROL_RE.sub("", text)


def sanitize_structure(obj: Any) -> Any:
    """Recursively strip control characters from all strings."""
    if isinstance(obj, dict):
        return {key: sanitize_structure(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize_structure(item) for item in obj]
    if isinstance(obj, str):
        return sanitize_text(obj)
    return obj


def collapse_whitespace(text: str) -> str:
    """Collapse all whitespace runs to single spaces (for summaries)."""
    return re.sub(r"\s+", " ", text).strip()
