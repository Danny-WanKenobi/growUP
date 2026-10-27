"""Shared agent-facing observability API (MVP-004C-A).

Agents call :func:`emit` (or the convenience helpers) with a typed
:class:`AgentEvent`. Routing is by retention class, invisible to the
caller: diagnostic events go to the JSONL trace, activity events go to
the trace plus the PostgreSQL index, and permanent events go to
PostgreSQL plus the trace (PostgreSQL is the durable copy).

Durability contract:

- File-sink failures never raise and never break business operations;
  they are echoed to stderr and counted (see :func:`get_status`).
- Activity PostgreSQL failures degrade to file-only with the same
  observable accounting.
- Permanent PostgreSQL failures raise :class:`EventPersistenceError`
  after a best-effort file copy, so they are never silently reported
  as successful. Callers performing significant decisions must handle
  that failure explicitly.
- :func:`emit_nonfatal` never raises on durability errors; instead it
  returns an :class:`EmitOutcome` whose ``persisted_pg``/``degraded``
  flags tell the caller exactly what was durably recorded. A degraded
  permanent event is explicitly NOT durably recorded -- the file copy
  is a trace, not a substitute, and a linked warning records the gap.
- Internal error paths use stderr and counters only; they never emit
  events, so logging cannot recurse on itself.
- PostgreSQL writes use dedicated short-lived connections, never
  business transactions.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability.config import ObservabilityConfig, get_observability_config
from app.observability.events import (
    AgentEvent,
    AgentId,
    EvidenceRef,
    RetentionClass,
    Severity,
)
from app.observability.jsonlog import JsonlSink, get_sink
from app.observability.redaction import (
    collapse_whitespace,
    redact,
    sanitize_structure,
    scrub_text,
)
from app.observability.store import insert_event


class EventPersistenceError(RuntimeError):
    """A permanent event could not be persisted to PostgreSQL."""


_pg_lock = threading.Lock()
_pg_written = 0
_pg_errors = 0
_pg_last_error: str | None = None


def _record_pg_write() -> None:
    global _pg_written
    with _pg_lock:
        _pg_written += 1


def _record_pg_error(message: str) -> None:
    global _pg_errors, _pg_last_error
    with _pg_lock:
        _pg_errors += 1
        _pg_last_error = message[:500]
    print(f"observability pg error: {message[:500]}", file=sys.stderr)


def get_pg_status() -> dict[str, Any]:
    """Observable PostgreSQL sink counters."""
    with _pg_lock:
        return {"written": _pg_written, "errors": _pg_errors, "last_error": _pg_last_error}


def get_status(config: ObservabilityConfig | None = None) -> dict[str, Any]:
    """Combined file + PostgreSQL sink status for health checks."""
    return {"file": get_sink(config).status(), "pg": get_pg_status()}


def _truncate_details(details: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    size = len(json.dumps(details, default=str).encode("utf-8"))
    if size <= max_bytes:
        return details
    return {
        "_truncated": True,
        "_original_bytes": size,
        "_keys": sorted(str(key) for key in details)[:50],
    }


def _prepare(event: AgentEvent, config: ObservabilityConfig) -> AgentEvent:
    """Redact, sanitize, and bound one event; returns a validated copy."""
    details = sanitize_structure(redact(event.details))
    details = _truncate_details(details, config.max_detail_bytes)
    refs = [
        EvidenceRef.model_validate(
            {key: scrub_text(str(value)) for key, value in ref.model_dump().items()}
        )
        for ref in event.evidence_refs
    ]
    if len(refs) > config.max_evidence_refs:
        omitted = len(refs) - config.max_evidence_refs
        refs = refs[: config.max_evidence_refs]
        details = dict(details)
        details["_evidence_refs_truncated"] = omitted
    summary = scrub_text(collapse_whitespace(event.summary))
    if not summary:
        summary = "(empty summary)"
    return AgentEvent.model_validate(
        {
            **event.model_dump(),
            "summary": summary,
            "details": details,
            "evidence_refs": [ref.model_dump() for ref in refs],
        }
    )


class EmitOutcome(BaseModel):
    """Honest record of what one emit actually persisted.

    ``persisted_pg`` is True only when PostgreSQL acknowledged the
    write; ``file_copy`` only when the JSONL trace accepted it.
    ``degraded`` is True only for permanent events that did NOT reach
    PostgreSQL -- a degraded permanent event is explicitly NOT durably
    recorded, and the caller must treat it as unpersisted.
    """

    event: AgentEvent
    persisted_pg: bool = False
    file_copy: bool = False
    degraded: bool = False
    error: str | None = None


def _persist(
    prepared: AgentEvent,
    config: ObservabilityConfig,
    sink: JsonlSink | None,
) -> tuple[bool, bool, Exception | None]:
    """Write one prepared event to its sinks. Never raises.

    Returns ``(file_copy, persisted_pg, pg_error)``. Diagnostic events
    never attempt PostgreSQL. Counters and stderr observe failures.
    """
    writer = sink if sink is not None else get_sink(config)
    file_ok = writer.write(prepared.to_record())
    if prepared.retention_class is RetentionClass.DIAGNOSTIC:
        return file_ok, False, None
    try:
        insert_event(prepared)
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as exc:
        _record_pg_error(f"{type(exc).__name__}: {exc}")
        return file_ok, False, exc
    _record_pg_write()
    return file_ok, True, None


def emit(
    event: AgentEvent,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> AgentEvent:
    """Validate, scrub, route, and persist one event. See module contract.

    When the resolved config is disabled (``OBS_ENABLED=0``), the event
    is still validated and scrubbed, but no sink is touched. Explicitly
    passed configs default to enabled, so tests with their own sinks are
    unaffected by the ambient switch.

    A permanent PostgreSQL failure raises :class:`EventPersistenceError`;
    callers that must not abort on durability errors use
    :func:`emit_nonfatal` and inspect the returned outcome instead.
    """
    cfg = config or get_observability_config()
    prepared = _prepare(event, cfg)
    if not cfg.enabled:
        return prepared
    _, pg_ok, pg_error = _persist(prepared, cfg, sink)
    if prepared.retention_class is RetentionClass.PERMANENT and not pg_ok:
        raise EventPersistenceError(
            f"permanent event {prepared.event_id} not persisted"
        ) from pg_error
    return prepared


def emit_nonfatal(
    event: AgentEvent,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> EmitOutcome:
    """Emit without ever failing the caller on durability errors.

    Used at business-operation boundaries (research, intake): a
    permanent-event persistence failure is downgraded to a file-traced
    activity warning instead of aborting the operation -- but the
    returned :class:`EmitOutcome` reports ``degraded=True`` and
    ``persisted_pg=False``, so degraded permanence is never mistaken
    for durable recording. Validation errors still propagate --
    malformed events are code bugs that must fail fast in development
    and testing.
    """
    cfg = config or get_observability_config()
    prepared = _prepare(event, cfg)
    if not cfg.enabled:
        return EmitOutcome(
            event=prepared,
            degraded=prepared.retention_class is RetentionClass.PERMANENT,
            error="observability disabled",
        )
    file_ok, pg_ok, pg_error = _persist(prepared, cfg, sink)
    degraded = prepared.retention_class is RetentionClass.PERMANENT and not pg_ok
    if degraded:
        fallback = AgentEvent(
            agent=prepared.agent,
            run_id=prepared.run_id,
            event_type="observability.persistence_degraded",
            summary=(
                f"permanent event {prepared.event_id} NOT durably recorded; "
                "file trace copy only"
            ),
            severity=Severity.WARNING,
            retention_class=RetentionClass.ACTIVITY,
            details={
                "original_event_id": prepared.event_id,
                "original_event_type": prepared.event_type,
                "pg_error": str(pg_error)[:300] if pg_error else "unknown",
                "file_copy": file_ok,
            },
            correlation_id=prepared.correlation_id,
            parent_event_id=prepared.event_id,
        )
        emit(fallback, config=cfg, sink=sink)  # activity: degrades, never raises
    return EmitOutcome(
        event=prepared,
        persisted_pg=pg_ok,
        file_copy=file_ok,
        degraded=degraded,
        error=str(pg_error)[:500] if pg_error else None,
    )


def _build(
    agent: AgentId | str,
    run_id: str,
    event_type: str,
    summary: str,
    severity: Severity,
    retention_class: RetentionClass,
    details: dict[str, Any] | None,
    correlation_id: str,
    property_id: UUID | str | None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple,
    parent_event_id: str | None,
) -> AgentEvent:
    refs = [
        ref if isinstance(ref, EvidenceRef) else EvidenceRef.model_validate(ref)
        for ref in evidence_refs
    ]
    return AgentEvent(
        agent=agent,  # type: ignore[arg-type] - pydantic coerces, rejects unknown
        run_id=run_id,
        event_type=event_type,
        summary=summary,
        severity=severity,
        retention_class=retention_class,
        details=dict(details or {}),
        correlation_id=correlation_id,
        property_id=property_id,  # type: ignore[arg-type] - pydantic coerces
        evidence_refs=refs,
        parent_event_id=parent_event_id,
    )


def log_diagnostic(
    agent: AgentId | str,
    run_id: str,
    event_type: str,
    summary: str,
    *,
    severity: Severity = Severity.DEBUG,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
    retention_class: RetentionClass = RetentionClass.DIAGNOSTIC,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> AgentEvent:
    """Emit a detailed trace event (file only, 90-day default retention)."""
    return emit(
        _build(
            agent, run_id, event_type, summary, severity, retention_class,
            details, correlation_id, property_id, evidence_refs, parent_event_id,
        ),
        config=config,
        sink=sink,
    )


def log_activity(
    agent: AgentId | str,
    run_id: str,
    event_type: str,
    summary: str,
    *,
    severity: Severity = Severity.INFO,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
    retention_class: RetentionClass = RetentionClass.ACTIVITY,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> AgentEvent:
    """Emit a routine activity event (trace + PostgreSQL index)."""
    return emit(
        _build(
            agent, run_id, event_type, summary, severity, retention_class,
            details, correlation_id, property_id, evidence_refs, parent_event_id,
        ),
        config=config,
        sink=sink,
    )


def log_decision(
    agent: AgentId | str,
    run_id: str,
    event_type: str,
    summary: str,
    *,
    severity: Severity = Severity.INFO,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
    retention_class: RetentionClass = RetentionClass.PERMANENT,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> AgentEvent:
    """Emit a significant decision (durable; PG failure raises)."""
    return emit(
        _build(
            agent, run_id, event_type, summary, severity, retention_class,
            details, correlation_id, property_id, evidence_refs, parent_event_id,
        ),
        config=config,
        sink=sink,
    )


def log_evidence(
    agent: AgentId | str,
    run_id: str,
    event_type: str,
    summary: str,
    *,
    severity: Severity = Severity.INFO,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
    retention_class: RetentionClass = RetentionClass.PERMANENT,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> AgentEvent:
    """Emit an evidence-reference event (durable; PG failure raises)."""
    return emit(
        _build(
            agent, run_id, event_type, summary, severity, retention_class,
            details, correlation_id, property_id, evidence_refs, parent_event_id,
        ),
        config=config,
        sink=sink,
    )


def log_handoff(
    agent: AgentId | str,
    run_id: str,
    to_agent: AgentId | str,
    summary: str,
    *,
    severity: Severity = Severity.INFO,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
    retention_class: RetentionClass = RetentionClass.PERMANENT,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> AgentEvent:
    """Emit an agent handoff (durable; PG failure raises)."""
    merged = dict(details or {})
    merged.setdefault("to_agent", to_agent.value if isinstance(to_agent, AgentId) else str(to_agent))
    return emit(
        _build(
            agent, run_id, "agent.handoff", summary, severity, retention_class,
            merged, correlation_id, property_id, evidence_refs, parent_event_id,
        ),
        config=config,
        sink=sink,
    )


def log_warning(
    agent: AgentId | str,
    run_id: str,
    event_type: str,
    summary: str,
    *,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
    retention_class: RetentionClass = RetentionClass.ACTIVITY,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> AgentEvent:
    """Emit a warning (trace + index; degrades to trace on PG failure)."""
    return emit(
        _build(
            agent, run_id, event_type, summary, Severity.WARNING, retention_class,
            details, correlation_id, property_id, evidence_refs, parent_event_id,
        ),
        config=config,
        sink=sink,
    )


def log_error(
    agent: AgentId | str,
    run_id: str,
    event_type: str,
    summary: str,
    *,
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
    property_id: UUID | str | None = None,
    evidence_refs: list[EvidenceRef | dict[str, Any]] | tuple = (),
    parent_event_id: str | None = None,
    retention_class: RetentionClass = RetentionClass.ACTIVITY,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> AgentEvent:
    """Emit an error (trace + index; degrades to trace on PG failure)."""
    return emit(
        _build(
            agent, run_id, event_type, summary, Severity.ERROR, retention_class,
            details, correlation_id, property_id, evidence_refs, parent_event_id,
        ),
        config=config,
        sink=sink,
    )
