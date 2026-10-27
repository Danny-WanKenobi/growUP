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
  returns an :class:`EmitOutcome` whose flags tell the caller exactly
  what was durably recorded. With an outbox, permanent events take the
  outbox-first path (outbox commit, immediate delivery, mark
  delivered); ``degraded`` then means durable in one store but with
  follow-up still owed (outbox-durable awaiting PostgreSQL, or
  PostgreSQL-persisted with failed outbox bookkeeping), and
  ``failed`` means no durable store accepted the event. The JSONL
  file copy is a trace, never durability. Once the outbox accepts an
  event, later ``record_attempt``/``mark_delivered`` failures are
  bookkeeping: counted for monitoring (see :func:`get_outbox_status`)
  and noted on the outcome, but they never downgrade durable flags.
- :func:`require_durable` raises :class:`DurabilityError` when a
  permanent event reaches neither durable store. Consequential
  operations must fail closed on it -- but a durable event write is
  still not transactional consistency with business tables.
- The legacy helpers (:func:`emit`, :func:`log_decision`,
  :func:`log_evidence`, :func:`log_handoff`, and :func:`emit_nonfatal`
  without an outbox) remain PostgreSQL-only: they never touch the
  SQLite outbox. Consequential operations must explicitly call
  :func:`require_durable` with a caller-supplied outbox; passing no
  outbox keeps even :func:`require_durable` PostgreSQL-only (it still
  raises when PostgreSQL rejects the event).
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
from app.observability.outbox import Outbox, OutboxConflictError, OutboxError
from app.observability.redaction import (
    collapse_whitespace,
    redact,
    sanitize_structure,
    scrub_text,
)
from app.observability.store import deliver_event, insert_event


class EventPersistenceError(RuntimeError):
    """A permanent event could not be persisted to PostgreSQL."""


class DurabilityError(RuntimeError):
    """A permanent event reached no durable store (neither PostgreSQL
    nor the SQLite outbox). Business operations requiring durable
    acceptance must fail closed on this error."""


_pg_lock = threading.Lock()
_pg_written = 0
_pg_errors = 0
_pg_last_error: str | None = None

_outbox_lock = threading.Lock()
_outbox_errors = 0
_outbox_last_error: str | None = None


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


def _record_outbox_error(message: str) -> None:
    """Count and echo an outbox bookkeeping failure (monitoring only)."""
    global _outbox_errors, _outbox_last_error
    with _outbox_lock:
        _outbox_errors += 1
        _outbox_last_error = message[:500]
    print(f"observability outbox error: {message[:500]}", file=sys.stderr)


def get_outbox_status() -> dict[str, Any]:
    """Observable outbox bookkeeping-failure counters."""
    with _outbox_lock:
        return {"errors": _outbox_errors, "last_error": _outbox_last_error}


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
    write; ``persisted_outbox`` only after a committed outbox accept;
    ``file_copy`` only when the JSONL trace accepted it. The JSONL
    trace is NEVER durability: ``degraded`` means a permanent event is
    durable in the outbox but still awaiting PostgreSQL delivery, and
    ``failed`` means it reached no durable store at all.
    """

    event: AgentEvent
    persisted_pg: bool = False
    persisted_outbox: bool = False
    file_copy: bool = False
    degraded: bool = False
    failed: bool = False
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


def _degradation_warning(
    prepared: AgentEvent,
    file_ok: bool,
    detail: str,
    outbox_ok: bool,
    pg_ok: bool = False,
) -> AgentEvent:
    """File-traced warning recording a permanent durability gap."""
    if pg_ok:
        summary = (
            f"permanent event {prepared.event_id} persisted to PostgreSQL "
            "but outbox bookkeeping failed; redelivery confirms idempotently"
        )
    elif outbox_ok:
        summary = (
            f"permanent event {prepared.event_id} durable in outbox only; "
            "PostgreSQL delivery pending"
        )
    else:
        summary = (
            f"permanent event {prepared.event_id} NOT durably recorded; "
            "file trace copy only"
        )
    return AgentEvent(
        agent=prepared.agent,
        run_id=prepared.run_id,
        event_type="observability.persistence_degraded",
        summary=summary,
        severity=Severity.WARNING,
        retention_class=RetentionClass.ACTIVITY,
        details={
            "original_event_id": prepared.event_id,
            "original_event_type": prepared.event_type,
            "detail": detail[:300],
            "file_copy": file_ok,
            "persisted_outbox": outbox_ok,
            "persisted_pg": pg_ok,
        },
        correlation_id=prepared.correlation_id,
        parent_event_id=prepared.event_id,
    )


def _safe_record_attempt(outbox: Outbox, event_id: str, category: str) -> str | None:
    """Best-effort retry bookkeeping; returns an error note on failure."""
    try:
        outbox.record_attempt(event_id, category)
    except OutboxError as exc:
        note = f"outbox record_attempt failed: {exc}"
        _record_outbox_error(f"{event_id}: {note}")
        return note
    return None


def _with_note(primary: str, note: str | None) -> str:
    """Combine a primary error with an optional bookkeeping note."""
    if note:
        return f"{primary}; {note}"[:500]
    return primary[:500]


def _accept_durable(
    prepared: AgentEvent,
    cfg: ObservabilityConfig,
    sink: JsonlSink | None,
    outbox: Outbox,
) -> EmitOutcome:
    """Outbox-first permanent acceptance (crash cannot strand events).

    Sequence: file trace copy, outbox commit, immediate PostgreSQL
    delivery, outbox mark-delivered. A crash after the outbox commit
    leaves the event for the recovery worker; a crash after PostgreSQL
    delivery but before marking resolves idempotently on redelivery.

    Once ``outbox.accept`` succeeds the event IS durably accepted:
    later ``record_attempt``/``mark_delivered`` failures are
    bookkeeping, never acceptance failure. They are recorded for
    monitoring and noted on the outcome, but the durable flags stay
    truthful. An event is marked delivered only after confirmed
    PostgreSQL delivery.
    """
    writer = sink if sink is not None else get_sink(cfg)
    file_ok = writer.write(prepared.to_record())
    try:
        outbox.accept(prepared)
    except OutboxConflictError as exc:
        # Same ID, different payload already stored: fail closed without
        # touching PostgreSQL, so the divergence cannot spread.
        return EmitOutcome(
            event=prepared,
            file_copy=file_ok,
            failed=True,
            error=f"outbox conflict: {exc}"[:500],
        )
    except OutboxError as exc:
        return _direct_pg_fallback(prepared, cfg, sink, file_ok, f"outbox unavailable: {exc}")
    try:
        delivered = deliver_event(prepared)
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as exc:
        note = _safe_record_attempt(outbox, prepared.event_id, _error_category(exc))
        return EmitOutcome(
            event=prepared,
            persisted_outbox=True,
            file_copy=file_ok,
            degraded=True,
            error=_with_note(f"pg unavailable: {exc}", note),
        )
    if delivered.status == "conflict":
        note = _safe_record_attempt(outbox, prepared.event_id, "pg_content_conflict")
        return EmitOutcome(
            event=prepared,
            persisted_outbox=True,
            file_copy=file_ok,
            degraded=True,
            error=_with_note(
                "pg_content_conflict: PostgreSQL holds different content for this event ID",
                note,
            ),
        )
    try:
        outbox.mark_delivered(prepared.event_id)
    except OutboxError as exc:
        note = f"outbox mark_delivered failed: {exc}"
        _record_outbox_error(f"{prepared.event_id}: {note}")
        # PostgreSQL committed; the row stays pending and the recovery
        # worker confirms it idempotently on redelivery.
        return EmitOutcome(
            event=prepared,
            persisted_pg=True,
            persisted_outbox=True,
            file_copy=file_ok,
            degraded=True,
            error=_with_note("pg persisted", note),
        )
    return EmitOutcome(
        event=prepared, persisted_pg=True, persisted_outbox=True, file_copy=file_ok
    )


def _direct_pg_fallback(
    prepared: AgentEvent,
    cfg: ObservabilityConfig,
    sink: JsonlSink | None,
    file_ok: bool,
    outbox_error: str,
) -> EmitOutcome:
    """Last resort when the outbox itself is unavailable: try PG once."""
    try:
        delivered = deliver_event(prepared)
    except (DatabaseNotConfiguredError, DatabaseUnavailableError) as exc:
        return EmitOutcome(
            event=prepared,
            file_copy=file_ok,
            failed=True,
            error=f"{outbox_error}; pg unavailable: {exc}"[:500],
        )
    if delivered.status == "conflict":
        return EmitOutcome(
            event=prepared,
            file_copy=file_ok,
            failed=True,
            error=f"{outbox_error}; pg_content_conflict"[:500],
        )
    return EmitOutcome(event=prepared, persisted_pg=True, file_copy=file_ok)


def _error_category(exc: BaseException) -> str:
    if isinstance(exc, DatabaseNotConfiguredError):
        return "pg_not_configured"
    return "pg_unreachable"


def emit_nonfatal(
    event: AgentEvent,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
    outbox: Outbox | None = None,
) -> EmitOutcome:
    """Emit without ever failing the caller on durability errors.

    Ordinary research path (``outbox=None``): legacy behavior -- PG
    best-effort with file trace, and a file-traced warning when a
    permanent event reaches no durable store. With an outbox, permanent
    events take the outbox-first durable path instead. Either way the
    returned :class:`EmitOutcome` distinguishes PostgreSQL, outbox,
    file-only, degraded, and failed results. Validation errors still
    propagate -- malformed events are code bugs that must fail fast in
    development and testing.
    """
    cfg = config or get_observability_config()
    prepared = _prepare(event, cfg)
    if not cfg.enabled:
        permanent = prepared.retention_class is RetentionClass.PERMANENT
        return EmitOutcome(
            event=prepared,
            failed=permanent,
            error="observability disabled" if permanent else None,
        )
    if outbox is not None and prepared.retention_class is RetentionClass.PERMANENT:
        outcome = _accept_durable(prepared, cfg, sink, outbox)
    else:
        file_ok, pg_ok, pg_error = _persist(prepared, cfg, sink)
        failed = prepared.retention_class is RetentionClass.PERMANENT and not pg_ok
        outcome = EmitOutcome(
            event=prepared,
            persisted_pg=pg_ok,
            file_copy=file_ok,
            failed=failed,
            error=str(pg_error)[:500] if pg_error else None,
        )
    if outcome.failed or outcome.degraded:
        emit(
            _degradation_warning(
                prepared,
                outcome.file_copy,
                outcome.error or "unknown",
                outcome.persisted_outbox,
                outcome.persisted_pg,
            ),
            config=cfg,
            sink=sink,
        )  # activity: degrades, never raises
    return outcome


def require_durable(
    event: AgentEvent,
    outbox: Outbox | None = None,
    config: ObservabilityConfig | None = None,
    sink: JsonlSink | None = None,
) -> EmitOutcome:
    """Require durable acceptance of a permanent event (fail closed).

    Returns the :class:`EmitOutcome` when the event reached PostgreSQL
    or (with an outbox) the SQLite outbox. Raises :class:`DurabilityError`
    when a permanent event reached neither durable store -- callers
    must abort the consequential operation. Non-permanent events take
    the ordinary nonfatal path and never raise for durability.

    A durable event write alone is NOT transactional consistency with
    business tables or external systems: consequential workflows still
    need coordinated state transitions, idempotency, and recovery.
    """
    outcome = emit_nonfatal(event, config=config, sink=sink, outbox=outbox)
    if outcome.failed:
        raise DurabilityError(
            f"permanent event {outcome.event.event_id} reached no durable store: "
            f"{outcome.error or 'unknown'}"
        )
    return outcome


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
