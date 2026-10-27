"""Transaction-safe consequential auditing (Phase 3C-2).

:func:`run_consequential_transaction` commits one caller-supplied
PostgreSQL business mutation and its permanent audit event atomically:
both commit together on the coordinator-owned connection, or both
roll back. This is the guarantee Phase 3C-1 deliberately deferred --
durable event acceptance alone says nothing about whether a business
transaction committed.

What this module does NOT do:

- No external calls: no outreach, payments, or contractual actions.
- No SQLite or JSONL fallback: if PostgreSQL fails, the operation
  fails closed with :class:`DatabaseUnavailableError`. SQLite cannot
  join a PostgreSQL transaction, so an outbox accept here would be a
  lie about atomicity, not a substitute for it.
- No standalone emit path: the audit INSERT runs inside the business
  transaction via :func:`insert_event_in_transaction`, never through
  ``audit_event``/``require_durable``/``emit_nonfatal`` (those open
  their own connections and cannot join this transaction).
- No external-action atomicity: for consequential *external* effects
  this commits the audit record only; the authorization/attempt/
  outcome lifecycle with idempotency belongs to Phase 3C-3.

Callback contract (transaction ownership):

- The coordinator owns the connection and the transaction. It opens
  exactly one connection, passes it to ``mutate``, runs the audit
  INSERT on it, then commits. On any failure it rolls back and
  closes; the business result is returned only after commit.
- The callback receives the open connection and must use it for its
  writes. It must NOT commit, roll back, close, or replace the
  connection, stash it for use after return, or open side
  transactions the coordinator cannot see.
- A generic coordinator cannot reliably prevent a hostile or buggy
  callback from manipulating its own connection object, so misuse
  voids the atomicity guarantee by definition. Callers that commit
  early, swallow the connection, or write elsewhere are outside the
  supported contract. This is documented explicitly because no
  wrapper can enforce it.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability.events import AgentEvent, RetentionClass
from app.observability.policy import OperationClass, policy_for
from app.observability.store import insert_event_in_transaction
from app.properties_db import get_connection

_T = TypeVar("_T")


def _quiet_rollback(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass  # original failure already decided the outcome


def _quiet_close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass  # cleanup must not mask the real outcome


def run_consequential_transaction(
    *,
    operation_class: OperationClass,
    event: AgentEvent,
    mutate: Callable[[Any], _T],
    config: DatabaseConfig | None = None,
) -> _T:
    """Commit one business mutation and its audit event atomically.

    Opens exactly one PostgreSQL connection, runs ``mutate(conn)``,
    executes the permanent audit INSERT on the same connection, and
    commits only after both succeed. Any failure rolls both back and
    raises -- success is reported only after commit.

    Classification and event are validated BEFORE any connection is
    opened: only consequential classes are accepted, and only
    permanent events. ``mutate`` never runs when validation or
    connection setup fails.

    Error behavior: the callback's own exceptions propagate unchanged
    after rollback (they are the caller's errors, with context
    intact). Infrastructure failures -- audit INSERT errors and
    commit failures -- raise :class:`DatabaseUnavailableError` with a
    short sanitized code and the original exception chained. No
    credentials, connection strings, or payloads appear in messages.

    See the module docstring for the callback transaction-ownership
    contract: the callback must not commit, roll back, close, or
    replace the supplied connection.
    """
    policy = policy_for(operation_class)  # TypeError for strings/unknowns
    if not policy.requires_durable_audit:
        raise ValueError(
            "transaction coordinator requires a consequential classification, "
            f"got {operation_class.value}"
        )
    if not isinstance(event, AgentEvent):
        raise TypeError("event must be an AgentEvent")
    if event.retention_class is not RetentionClass.PERMANENT:
        raise ValueError("transactional audit requires a permanent event")
    if not callable(mutate):
        raise TypeError("mutate must be callable")
    conn = get_connection(config)  # raises before any business runs
    try:
        result = mutate(conn)
    except Exception:
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise
    try:
        insert_event_in_transaction(conn, event)
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise
    except Exception as exc:  # pragma: no cover - helper already wraps driver errors
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise DatabaseUnavailableError("audit_insert_failed") from exc
    try:
        conn.commit()
    except Exception as exc:
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise DatabaseUnavailableError("transaction_commit_failed") from exc
    _quiet_close(conn)
    return result
