"""Shared agent observability: structured events, JSONL trace, PG index.

All seven agents (Gabriel, Raphael, Onesimus, Wondrue, Michael, Uriel,
Priscilla) emit through this package. Agents never choose sinks: the
event's retention class routes it. See ``emitter`` for the durability
contract and ``store`` for Wondrue's future query surface.
"""

from __future__ import annotations

from app.observability.config import ObservabilityConfig, get_observability_config
from app.observability.emitter import (
    EventPersistenceError,
    emit,
    get_pg_status,
    get_status,
    log_activity,
    log_decision,
    log_diagnostic,
    log_error,
    log_evidence,
    log_handoff,
    log_warning,
)
from app.observability.events import (
    AgentEvent,
    AgentId,
    EvidenceRef,
    RetentionClass,
    Severity,
)
from app.observability.jsonlog import JsonlSink, get_sink, purge_old_log_files, reset_sinks
from app.observability.redaction import REDACTED, redact, scrub_text
from app.observability.store import (
    by_correlation,
    by_property,
    by_run,
    decisions,
    handoffs,
    insert_event,
    purge_expired_events,
    query_events,
    recent_by_agent,
)

__all__ = [
    "AgentEvent",
    "AgentId",
    "EvidenceRef",
    "EventPersistenceError",
    "JsonlSink",
    "ObservabilityConfig",
    "REDACTED",
    "RetentionClass",
    "Severity",
    "by_correlation",
    "by_property",
    "by_run",
    "decisions",
    "emit",
    "get_observability_config",
    "get_pg_status",
    "get_sink",
    "get_status",
    "handoffs",
    "insert_event",
    "log_activity",
    "log_decision",
    "log_diagnostic",
    "log_error",
    "log_evidence",
    "log_handoff",
    "log_warning",
    "purge_expired_events",
    "purge_old_log_files",
    "query_events",
    "recent_by_agent",
    "redact",
    "reset_sinks",
    "scrub_text",
]
