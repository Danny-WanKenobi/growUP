"""Shared agent observability: structured events, JSONL trace, PG index.

All seven agents (Gabriel, Raphael, Onesimus, Wondrue, Michael, Uriel,
Priscilla) emit through this package. Agents never choose sinks: the
event's retention class routes it. See ``emitter`` for the durability
contract and ``store`` for Wondrue's future query surface.
"""

from __future__ import annotations

from app.observability.config import ObservabilityConfig, get_observability_config
from app.observability.emitter import (
    BatchEmitOutcome,
    DurabilityError,
    EmitOutcome,
    EventPersistenceError,
    emit,
    emit_nonfatal,
    get_outbox_status,
    get_pg_status,
    get_status,
    log_activity,
    log_decision,
    log_diagnostic,
    log_error,
    log_evidence,
    log_handoff,
    log_warning,
    require_durable,
    require_durable_batch,
)
from app.observability.events import (
    AgentEvent,
    AgentId,
    EvidenceRef,
    RetentionClass,
    Severity,
)
from app.observability.jsonlog import JsonlSink, get_sink, purge_old_log_files, reset_sinks
from app.observability.outbox import (
    Outbox,
    OutboxConfigError,
    OutboxConflictError,
    OutboxError,
    OutboxUnavailableError,
    PendingEvent,
    retry_delay_seconds,
    validate_production_path,
)
from app.observability.policy import (
    OperationClass,
    OperationPolicy,
    audit_event,
    policy_for,
)
from app.observability.redaction import REDACTED, redact, scrub_text
from app.observability.store import (
    DeliveryResult,
    by_correlation,
    by_property,
    by_run,
    decisions,
    deliver_event,
    handoffs,
    insert_event,
    insert_event_batch,
    insert_event_in_transaction,
    purge_expired_events,
    query_events,
    recent_by_agent,
)
from app.observability.transactions import run_consequential_transaction

__all__ = [
    "AgentEvent",
    "AgentId",
    "BatchEmitOutcome",
    "DeliveryResult",
    "DurabilityError",
    "EvidenceRef",
    "EmitOutcome",
    "EventPersistenceError",
    "JsonlSink",
    "ObservabilityConfig",
    "OperationClass",
    "OperationPolicy",
    "Outbox",
    "OutboxConfigError",
    "OutboxConflictError",
    "OutboxError",
    "OutboxUnavailableError",
    "PendingEvent",
    "REDACTED",
    "RetentionClass",
    "Severity",
    "audit_event",
    "by_correlation",
    "by_property",
    "by_run",
    "decisions",
    "deliver_event",
    "emit",
    "emit_nonfatal",
    "get_observability_config",
    "get_outbox_status",
    "get_pg_status",
    "get_sink",
    "get_status",
    "handoffs",
    "insert_event",
    "insert_event_batch",
    "insert_event_in_transaction",
    "log_activity",
    "log_decision",
    "log_diagnostic",
    "log_error",
    "log_evidence",
    "log_handoff",
    "log_warning",
    "policy_for",
    "purge_expired_events",
    "purge_old_log_files",
    "query_events",
    "recent_by_agent",
    "redact",
    "require_durable",
    "require_durable_batch",
    "reset_sinks",
    "retry_delay_seconds",
    "run_consequential_transaction",
    "scrub_text",
    "validate_production_path",
]
