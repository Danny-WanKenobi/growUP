"""Observability configuration from the environment (MVP-004C-A).

All settings are optional with safe defaults; invalid values fail
closed to the default rather than disabling safeguards. ``.env`` loading
is handled centrally by :mod:`app.config`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_DIAGNOSTIC_RETENTION_DAYS = 90
DEFAULT_ACTIVITY_RETENTION_DAYS = 365
DEFAULT_MAX_DETAIL_BYTES = 8192
DEFAULT_MAX_EVIDENCE_REFS = 50
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 10

_VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class ObservabilityConfig:
    """File sink, retention, and payload-bound settings (no secrets)."""

    enabled: bool = True
    log_dir: str = DEFAULT_LOG_DIR
    log_level: str = DEFAULT_LOG_LEVEL
    diagnostic_retention_days: int = DEFAULT_DIAGNOSTIC_RETENTION_DAYS
    activity_retention_days: int = DEFAULT_ACTIVITY_RETENTION_DAYS
    max_detail_bytes: int = DEFAULT_MAX_DETAIL_BYTES
    max_evidence_refs: int = DEFAULT_MAX_EVIDENCE_REFS
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_backup_count: int = DEFAULT_LOG_BACKUP_COUNT


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int((raw or "").strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return value if value > 0 else default


def get_observability_config() -> ObservabilityConfig:
    """Build observability settings from the current environment."""
    level = (os.getenv("OBS_LOG_LEVEL", DEFAULT_LOG_LEVEL) or "").strip().upper()
    enabled_raw = (os.getenv("OBS_ENABLED", "1") or "").strip().lower()
    return ObservabilityConfig(
        enabled=enabled_raw not in ("0", "false", "no", "off"),
        log_dir=os.getenv("OBS_LOG_DIR", DEFAULT_LOG_DIR) or DEFAULT_LOG_DIR,
        log_level=level if level in _VALID_LEVELS else DEFAULT_LOG_LEVEL,
        diagnostic_retention_days=_positive_int(
            os.getenv("OBS_DIAGNOSTIC_RETENTION_DAYS"), DEFAULT_DIAGNOSTIC_RETENTION_DAYS
        ),
        activity_retention_days=_positive_int(
            os.getenv("OBS_ACTIVITY_RETENTION_DAYS"), DEFAULT_ACTIVITY_RETENTION_DAYS
        ),
        max_detail_bytes=_positive_int(os.getenv("OBS_MAX_DETAIL_BYTES"), DEFAULT_MAX_DETAIL_BYTES),
        max_evidence_refs=_positive_int(
            os.getenv("OBS_MAX_EVIDENCE_REFS"), DEFAULT_MAX_EVIDENCE_REFS
        ),
        log_max_bytes=_positive_int(os.getenv("OBS_LOG_MAX_BYTES"), DEFAULT_LOG_MAX_BYTES),
        log_backup_count=_positive_int(
            os.getenv("OBS_LOG_BACKUP_COUNT"), DEFAULT_LOG_BACKUP_COUNT
        ),
    )
