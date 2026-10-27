"""Shared agent event model (MVP-004C-A, Phase 1).

One typed schema for all seven agents. Events carry identity (agent,
run, correlation, optional property), a stable machine-readable type,
severity, a concise summary, bounded structured details, evidence
references, and a retention class that decides durability.

Absolute safety ceilings live here (oversized payloads are rejected as
programmer errors); operational tuning lives in the emitter config,
which truncates with an explicit marker before these ceilings bite.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

# Absolute ceilings (never exceeded, regardless of operational config).
ABSOLUTE_MAX_SUMMARY_CHARS = 2000
ABSOLUTE_MAX_DETAIL_BYTES = 65536
ABSOLUTE_MAX_EVIDENCE_REFS = 200
ABSOLUTE_MAX_REF_CHARS = 2000
ABSOLUTE_MAX_ID_CHARS = 128

_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.]{1,63}$")


class AgentId(str, Enum):
    """Canonical agent identifiers (all seven agents share this)."""

    GABRIEL = "gabriel"
    RAPHAEL = "raphael"
    ONESIMUS = "onesimus"
    WONDRUE = "wondrue"
    MICHAEL = "michael"
    URIEL = "uriel"
    PRISCILLA = "priscilla"


class Severity(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class RetentionClass(str, Enum):
    DIAGNOSTIC = "diagnostic"  # file trace, 90-day default retention
    ACTIVITY = "activity"  # routine history, configurable retention
    PERMANENT = "permanent"  # decisions/evidence/handoffs, never auto-deleted


class EvidenceRef(BaseModel):
    """A pointer to supporting evidence (never the payload itself)."""

    kind: str = Field(min_length=1, max_length=64)
    ref: str = Field(min_length=1, max_length=ABSOLUTE_MAX_REF_CHARS)

    @field_validator("kind", "ref", mode="before")
    @classmethod
    def _strip_nulls(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("\x00", "")
        return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_event_id() -> str:
    return uuid.uuid4().hex


class AgentEvent(BaseModel):
    """One validated, serializable agent activity event."""

    event_id: str = Field(default_factory=_new_event_id, min_length=8, max_length=ABSOLUTE_MAX_ID_CHARS)
    timestamp: datetime = Field(default_factory=_utcnow)
    agent: AgentId
    run_id: str = Field(min_length=1, max_length=ABSOLUTE_MAX_ID_CHARS)
    correlation_id: str = Field(default="", max_length=ABSOLUTE_MAX_ID_CHARS)
    property_id: UUID | None = None
    event_type: str = Field(min_length=2, max_length=64)
    severity: Severity = Severity.INFO
    summary: str = Field(min_length=1, max_length=ABSOLUTE_MAX_SUMMARY_CHARS)
    details: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    parent_event_id: str | None = Field(default=None, max_length=ABSOLUTE_MAX_ID_CHARS)
    retention_class: RetentionClass = RetentionClass.ACTIVITY

    @field_validator("timestamp", mode="before")
    @classmethod
    def _coerce_utc(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        raise ValueError("timestamp must be a datetime or ISO string")

    @field_validator("event_type")
    @classmethod
    def _stable_name(cls, value: str) -> str:
        if not _EVENT_TYPE_RE.match(value):
            raise ValueError("event_type must match ^[a-z][a-z0-9_.]{1,63}$")
        return value

    @field_validator("summary", "run_id", "correlation_id", mode="before")
    @classmethod
    def _strip_nulls(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("\x00", "")
        return value

    @model_validator(mode="after")
    def _check_absolute_ceilings(self):
        if len(self.evidence_refs) > ABSOLUTE_MAX_EVIDENCE_REFS:
            raise ValueError("too many evidence_refs")
        try:
            size = len(json.dumps(self.details, default=str).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("details must be JSON-serializable") from exc
        if size > ABSOLUTE_MAX_DETAIL_BYTES:
            raise ValueError("details exceed absolute size ceiling")
        return self

    def to_record(self) -> dict[str, Any]:
        """JSON-compatible record for file and database sinks."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "agent": self.agent.value,
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "property_id": str(self.property_id) if self.property_id else None,
            "event_type": self.event_type,
            "severity": self.severity.value,
            "summary": self.summary,
            "details": json.loads(json.dumps(self.details, default=str)),
            "evidence_refs": [ref.model_dump() for ref in self.evidence_refs],
            "parent_event_id": self.parent_event_id,
            "retention_class": self.retention_class.value,
        }
