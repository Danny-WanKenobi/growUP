"""PostgreSQL activity-event index (MVP-004C-A).

Durable store for routine activity and permanent decision, evidence,
and handoff events. Uses the existing ``properties_db.get_connection``
convention with short-lived dedicated connections: event writes never
join business transactions and never hold them open -- except through
:func:`insert_event_in_transaction`, which executes the audit INSERT
on a caller-owned connection so a Phase 3C-2 coordinator can commit
business and audit writes atomically.

All queries are parameterized, bounded (limit clamped), and ordered
deterministically by ``(occurred_at, event_id)`` so pagination is
stable. Retention cleanup carries an explicit SQL-level guard that
permanent rows can never match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability.events import AgentEvent, RetentionClass
from app.properties_db import get_connection

DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 1000

_INSERT_COLUMNS = (
    "event_id",
    "occurred_at",
    "agent",
    "run_id",
    "correlation_id",
    "property_id",
    "event_type",
    "severity",
    "summary",
    "details",
    "evidence_refs",
    "parent_event_id",
    "retention_class",
)

_SELECT_COLUMNS = ", ".join(_INSERT_COLUMNS)


def _row_to_event(row: dict[str, Any]) -> AgentEvent:
    return AgentEvent.model_validate(
        {
            "event_id": row["event_id"],
            "timestamp": row["occurred_at"],
            "agent": row["agent"],
            "run_id": row["run_id"],
            "correlation_id": row.get("correlation_id") or "",
            "property_id": row.get("property_id"),
            "event_type": row["event_type"],
            "severity": row["severity"],
            "summary": row["summary"],
            "details": row.get("details") or {},
            "evidence_refs": row.get("evidence_refs") or [],
            "parent_event_id": row.get("parent_event_id"),
            "retention_class": row["retention_class"],
        }
    )


def insert_event(event: AgentEvent, config: Any = None) -> AgentEvent:
    """Persist one event row on a dedicated short-lived connection."""
    params = _insert_params(event)
    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in _INSERT_COLUMNS)
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO agent_events ({columns}) VALUES ({placeholders})",
                    params,
                )
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("event_insert_failed") from exc
    return event


def insert_event_in_transaction(conn: Any, event: AgentEvent) -> AgentEvent:
    """Execute the audit INSERT on a caller-owned connection/transaction.

    Phase 3C-2 primitive: the INSERT runs inside the caller's
    transaction on the supplied psycopg connection. This function
    never opens another connection, never commits, never rolls back,
    and never touches SQLite or JSONL. A return means the INSERT
    executed -- NOT that it committed; only the transaction owner
    (the 3C-2 coordinator) may commit.

    Only permanent events are accepted, checked before any SQL runs.
    The INSERT is plain (no ``ON CONFLICT`` handling): a duplicate
    ``event_id`` raises instead of merging, confirming, or
    overwriting, so a colliding audit fails closed.
    """
    if not isinstance(event, AgentEvent):
        raise TypeError("event must be an AgentEvent")
    if event.retention_class is not RetentionClass.PERMANENT:
        raise ValueError("transactional audit requires a permanent event")
    params = _insert_params(event)
    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in _INSERT_COLUMNS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO agent_events ({columns}) VALUES ({placeholders})",
                params,
            )
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("event_insert_failed") from exc
    return event


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of one idempotent PostgreSQL delivery attempt."""

    status: Literal["inserted", "already_present", "conflict"]
    event_id: str


def _insert_params(event: AgentEvent) -> dict[str, Any]:
    record = event.to_record()
    return {
        "event_id": record["event_id"],
        "occurred_at": event.timestamp,
        "agent": record["agent"],
        "run_id": record["run_id"],
        "correlation_id": record["correlation_id"],
        "property_id": str(record["property_id"]) if record["property_id"] else None,
        "event_type": record["event_type"],
        "severity": record["severity"],
        "summary": record["summary"],
        "details": json.dumps(record["details"]),
        "evidence_refs": json.dumps(record["evidence_refs"]),
        "parent_event_id": record["parent_event_id"],
        "retention_class": record["retention_class"],
    }


def _canonical_record(event: AgentEvent) -> str:
    return json.dumps(event.to_record(), sort_keys=True)


def deliver_event(event: AgentEvent, config: Any = None) -> DeliveryResult:
    """Idempotently deliver one event to PostgreSQL (recovery path).

    Inserts the row, or -- when the event ID already exists -- verifies
    the stored content: identical content confirms ``already_present``,
    while differing content reports ``conflict`` WITHOUT overwriting
    the existing row. Dedicated short-lived connection.
    """
    params = _insert_params(event)
    columns = ", ".join(_INSERT_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in _INSERT_COLUMNS)
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO agent_events ({columns}) VALUES ({placeholders})"
                    " ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                    params,
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    conn.commit()
                    return DeliveryResult(status="inserted", event_id=event.event_id)
                cur.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM agent_events WHERE event_id = %(event_id)s",
                    {"event_id": event.event_id},
                )
                row = cur.fetchone()
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("event_deliver_failed") from exc
    if row is None:
        # Lost race: the row vanished between the conflicting insert
        # and the verification read. Retryable, not a conflict.
        raise DatabaseUnavailableError("event_deliver_race")
    stored = _canonical_record(_row_to_event(row))
    if stored == _canonical_record(event):
        return DeliveryResult(status="already_present", event_id=event.event_id)
    return DeliveryResult(status="conflict", event_id=event.event_id)


def query_events(
    *,
    agent: str | None = None,
    run_id: str | None = None,
    correlation_id: str | None = None,
    property_id: UUID | str | None = None,
    event_type: str | None = None,
    retention_class: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = DEFAULT_QUERY_LIMIT,
    offset: int = 0,
    newest_first: bool = True,
    config: Any = None,
) -> list[AgentEvent]:
    """Query the activity index with bounded, deterministic pagination.

    Every filter is optional; unknown combinations simply match nothing.
    Ordering is always ``(occurred_at, event_id)`` in one direction, so
    pages are stable across calls.
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    limit = min(limit, MAX_QUERY_LIMIT)
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if agent is not None:
        clauses.append("agent = %(agent)s")
        params["agent"] = agent
    if run_id is not None:
        clauses.append("run_id = %(run_id)s")
        params["run_id"] = run_id
    if correlation_id is not None:
        clauses.append("correlation_id = %(correlation_id)s")
        params["correlation_id"] = correlation_id
    if property_id is not None:
        clauses.append("property_id = %(property_id)s")
        params["property_id"] = str(property_id)
    if event_type is not None:
        clauses.append("event_type = %(event_type)s")
        params["event_type"] = event_type
    if retention_class is not None:
        clauses.append("retention_class = %(retention_class)s")
        params["retention_class"] = retention_class
    if since is not None:
        clauses.append("occurred_at >= %(since)s")
        params["since"] = since
    if until is not None:
        clauses.append("occurred_at <= %(until)s")
        params["until"] = until
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    direction = "DESC" if newest_first else "ASC"
    params["limit"] = limit
    params["offset"] = offset
    sql = (
        f"SELECT {_SELECT_COLUMNS} FROM agent_events {where} "
        f"ORDER BY occurred_at {direction}, event_id {direction} "
        "LIMIT %(limit)s OFFSET %(offset)s"
    )
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("event_query_failed") from exc
    return [_row_to_event(row) for row in (rows or [])]


# ---------------------------------------------------------------------------
# Wondrue's future read-only query surface (controlled, bounded helpers)
# ---------------------------------------------------------------------------


def recent_by_agent(agent: str, limit: int = DEFAULT_QUERY_LIMIT, config: Any = None) -> list[AgentEvent]:
    """Recent activity for one agent, newest first."""
    return query_events(agent=agent, limit=limit, config=config)


def by_property(property_id: UUID | str, limit: int = DEFAULT_QUERY_LIMIT, config: Any = None) -> list[AgentEvent]:
    """Activity associated with one property, newest first."""
    return query_events(property_id=property_id, limit=limit, config=config)


def by_run(run_id: str, limit: int = MAX_QUERY_LIMIT, config: Any = None) -> list[AgentEvent]:
    """Events belonging to one execution, oldest first for replay order."""
    return query_events(run_id=run_id, limit=limit, newest_first=False, config=config)


def by_correlation(correlation_id: str, limit: int = MAX_QUERY_LIMIT, config: Any = None) -> list[AgentEvent]:
    """Cross-agent history for one correlation ID, oldest first."""
    return query_events(correlation_id=correlation_id, limit=limit, newest_first=False, config=config)


def decisions(
    limit: int = DEFAULT_QUERY_LIMIT, agent: str | None = None, config: Any = None
) -> list[AgentEvent]:
    """Significant permanent decisions (and their evidence references)."""
    return query_events(retention_class="permanent", agent=agent, limit=limit, config=config)


def handoffs(
    limit: int = DEFAULT_QUERY_LIMIT, agent: str | None = None, config: Any = None
) -> list[AgentEvent]:
    """Agent handoff history, newest first."""
    return query_events(event_type="agent.handoff", agent=agent, limit=limit, config=config)


# ---------------------------------------------------------------------------
# Retention cleanup (permanent rows are unmatchable by construction)
# ---------------------------------------------------------------------------


def purge_expired_events(
    diagnostic_retention_days: int, activity_retention_days: int, config: Any = None
) -> dict[str, int]:
    """Delete expired diagnostic/activity rows. Permanent rows never match.

    Returns per-class deletion counts. The ``retention_class !=
    'permanent'`` guard is part of the statement itself, not caller
    discipline, so cleanup can never remove permanent records.
    """
    if diagnostic_retention_days < 1 or activity_retention_days < 1:
        raise ValueError("retention windows must be positive")
    counts: dict[str, int] = {}
    statements = (
        ("diagnostic", diagnostic_retention_days),
        ("activity", activity_retention_days),
    )
    try:
        with get_connection(config) as conn:
            with conn.cursor() as cur:
                for retention, days in statements:
                    cur.execute(
                        "DELETE FROM agent_events "
                        "WHERE retention_class = %(retention)s "
                        "AND retention_class != 'permanent' "
                        "AND occurred_at < now() - (%(days)s || ' days')::interval",
                        {"retention": retention, "days": days},
                    )
                    counts[retention] = cur.rowcount if cur.rowcount is not None else 0
            conn.commit()
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("event_purge_failed") from exc
    return counts
