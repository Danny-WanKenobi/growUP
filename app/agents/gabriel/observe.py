"""Gabriel observability presets over the shared emitter (MVP-004C-A-2).

Thin convenience over :mod:`app.observability` -- not a second logging
system. Presets ``agent=gabriel``, generates run identifiers, and
routes every permanent decision through :func:`emit_nonfatal` so a
durability failure degrades to a file-traced warning instead of
aborting research, investigation, ranking, dossier, or intake work.

Failure policy (all Gabriel boundaries are nonfatal):

- Diagnostic/activity emits never raise on sink failure.
- Permanent emits degrade via ``emit_nonfatal`` (file copy + warning),
  returning an outcome that distinguishes persisted from degraded.
- Event validation errors propagate: malformed events are code bugs.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from app.observability import (
    AgentEvent,
    AgentId,
    EmitOutcome,
    EvidenceRef,
    RetentionClass,
    Severity,
    emit_nonfatal,
    log_activity,
    log_diagnostic,
    log_warning,
)

GABRIEL = AgentId.GABRIEL


def new_run_id() -> str:
    """Generate a unique run identifier for one Gabriel execution."""
    return uuid.uuid4().hex


def note_diagnostic(
    run_id: str,
    event_type: str,
    summary: str,
    *,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    severity: Severity = Severity.DEBUG,
) -> AgentEvent:
    """File-traced diagnostic event (never raises on sink failure)."""
    return log_diagnostic(
        GABRIEL, run_id, event_type, summary,
        severity=severity, details=details, correlation_id=correlation_id,
    )


def note_activity(
    run_id: str,
    event_type: str,
    summary: str,
    *,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
) -> AgentEvent:
    """Routine activity event (trace + index; degrades to trace)."""
    return log_activity(
        GABRIEL, run_id, event_type, summary,
        details=details, correlation_id=correlation_id, property_id=property_id,
        evidence_refs=evidence_refs, parent_event_id=parent_event_id,
    )


def note_warning(
    run_id: str,
    event_type: str,
    summary: str,
    *,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
) -> AgentEvent:
    """Warning event (trace + index; degrades to trace)."""
    return log_warning(
        GABRIEL, run_id, event_type, summary,
        details=details, correlation_id=correlation_id, property_id=property_id,
    )


def note_decision(
    run_id: str,
    event_type: str,
    summary: str,
    *,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
) -> EmitOutcome:
    """Significant decision (permanent; durability failure degrades).

    Uses :func:`emit_nonfatal`: on PostgreSQL failure the decision is
    kept in the file trace and a warning records the degradation, so
    Gabriel's work is never aborted by observability. The returned
    outcome reports ``persisted_pg``/``degraded`` honestly -- a
    degraded outcome is explicitly NOT durably recorded.
    """
    refs = [
        ref if isinstance(ref, EvidenceRef) else EvidenceRef.model_validate(ref)
        for ref in evidence_refs
    ]
    return emit_nonfatal(
        AgentEvent(
            agent=GABRIEL,
            run_id=run_id,
            event_type=event_type,
            summary=summary,
            severity=Severity.INFO,
            retention_class=RetentionClass.PERMANENT,
            details=dict(details or {}),
            correlation_id=correlation_id,
            property_id=property_id,  # type: ignore[arg-type] - pydantic coerces
            evidence_refs=refs,
            parent_event_id=parent_event_id,
        )
    )
