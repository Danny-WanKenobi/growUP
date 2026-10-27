"""Consequential external-action ledger (MVP-004C-A Phase 3C-3A).

PostgreSQL-backed authorization records for consequential external
actions (outreach, contracts, external instructions). This module
records durable AUTHORIZATION only -- it never executes, retries, or
advances external actions. Execution belongs to Phase 3C-3B.

Authorization vs execution: a row in ``external_actions`` proves an
action was authorized at a point in time. It says nothing about
whether the action ran, is running, or succeeded. No state in this
phase implies execution; only creation (AUTHORIZED) exists.

Idempotency contract: every authorization carries a caller-chosen
unique ``idempotency_key`` plus a ``request_fingerprint`` (a
``sha256:``-prefixed hash over the canonical action type, agent,
property, immutable request details, authorizing principal, and
authorization reference). Resubmitting the same key with identical
request, principal, and reference returns the existing action
without a new row or a duplicate audit event. Reusing a key with a
different request, a different principal, or a different reference
raises :class:`IdempotencyConflictError` -- a conflicting submission
can never silently replay somebody else's authorization. Concurrency
safety comes from the PostgreSQL UNIQUE constraint plus
``ON CONFLICT DO NOTHING`` -- never check-then-insert.

Atomicity: a new authorization and its permanent audit event commit
in ONE PostgreSQL transaction, reusing
:func:`insert_event_in_transaction` on the coordinator-owned
connection. (The 3C-2 coordinator itself always audits, so the
replay path -- which must NOT write a duplicate audit -- uses the
underlying transactional primitive directly with the same
commit/rollback discipline.) If PostgreSQL is unavailable the call
fails closed: the SQLite observability outbox can preserve an audit
trail, but an outbox row can never authorize an external action,
because it cannot join the PostgreSQL authorization transaction.

Principal-verification LIMITATION (read before exposing this): the
``authorized_by`` value is a CLAIMED principal string. No
authenticated-principal verification exists yet, so a caller-supplied
string is recorded as claimed, never as proven. This interface is
internal-only: it must not be exposed through an API route or
represented as a fully secure authorization system until verified
principal binding exists. The claimed principal is bound into the
idempotency fingerprint, so a submission under a different principal
conflicts loudly instead of replaying another principal's row -- but
unpredictable per-authorization keys (e.g. UUIDs) remain required,
since key squatting can still force a victim's submission to
conflict.

Security: the raw ``request`` details are NEVER stored -- only the
fingerprint hash. Audit details carry validated identifiers and the
fingerprint, never credentials, payloads, or contact data. Callers
must still pass non-sensitive request details only: a fingerprint
does not make a sensitive value safe against low-entropy guessing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity
from app.observability.store import insert_event_in_transaction
from app.properties_db import get_connection

# Explicit ledger field bounds: fail closed on oversized input rather
# than truncating audit identity (keys feed the audit correlation ID).
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_ACTION_TYPE_CHARS = 128
MAX_PRINCIPAL_CHARS = 200
MAX_REFERENCE_CHARS = 200

_AUDIT_EVENT_TYPE = "external_action.authorized"

_ACTION_COLUMNS = (
    "action_id",
    "idempotency_key",
    "action_type",
    "agent",
    "property_id",
    "request_fingerprint",
    "authorized_by",
    "authorization_reference",
    "authorized_at",
    "state",
    "attempt_count",
    "created_at",
    "updated_at",
)
_SELECT_COLUMNS = ", ".join(_ACTION_COLUMNS)


class ActionState(str, Enum):
    """Lifecycle states for a consequential external action."""

    AUTHORIZED = "AUTHORIZED"  # recorded; execution not started (3C-3A creates only this)
    PENDING = "PENDING"  # queued for execution (3C-3B)
    ATTEMPTING = "ATTEMPTING"  # execution in flight (3C-3B)
    SUCCEEDED = "SUCCEEDED"  # confirmed completion (3C-3B, terminal)
    FAILED = "FAILED"  # confirmed failure; may requeue (3C-3B)
    UNKNOWN = "UNKNOWN"  # attempted, outcome unconfirmed (3C-3B)


ALLOWED_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.AUTHORIZED: frozenset({ActionState.PENDING}),
    ActionState.PENDING: frozenset({ActionState.ATTEMPTING, ActionState.FAILED}),
    ActionState.ATTEMPTING: frozenset(
        {ActionState.SUCCEEDED, ActionState.FAILED, ActionState.UNKNOWN}
    ),
    ActionState.FAILED: frozenset({ActionState.PENDING}),
    # UNKNOWN outcomes are terminal-until-reconciled: requeueing an
    # action whose execution may already have happened could double
    # the external side effect, so UNKNOWN -> PENDING is prohibited.
    # The only exits are verified reconciliation to a definitive
    # outcome (see validate_transition).
    ActionState.UNKNOWN: frozenset({ActionState.SUCCEEDED, ActionState.FAILED}),
    ActionState.SUCCEEDED: frozenset(),
}


def validate_transition(current: ActionState | str, new: ActionState | str) -> None:
    """Reject any lifecycle change outside the explicit policy.

    Unknown states and unlisted transitions raise ValueError. Phase
    3C-3A defines the policy only; no transition-executing function
    exists yet (3C-3B owns execution-adjacent writes). Two exits carry
    additional 3C-3B evidence obligations this validator cannot check:
    UNKNOWN -> SUCCEEDED/FAILED require independently verified
    reconciliation evidence (never a re-attempt), and FAILED ->
    PENDING requires explicit retry authorization plus proof of
    definitive failure.
    """
    try:
        current_state = current if isinstance(current, ActionState) else ActionState(current)
        new_state = new if isinstance(new, ActionState) else ActionState(new)
    except ValueError as exc:
        raise ValueError(f"invalid lifecycle state: {exc}") from exc
    if new_state not in ALLOWED_TRANSITIONS[current_state]:
        raise ValueError(
            f"transition {current_state.value} -> {new_state.value} is not permitted"
        )


class IdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different immutable request."""


@dataclass(frozen=True)
class AuthorizationResult:
    """Outcome of one authorization request (created vs replayed)."""

    action: dict[str, Any]
    created: bool


def canonical_request_fingerprint(
    action_type: str,
    agent: AgentId | str,
    property_id: UUID | str | None,
    request: dict[str, Any],
    authorized_by: str,
    authorization_reference: str,
) -> str:
    """Hash the canonical immutable request (deterministic, non-sensitive).

    Key order never affects the digest; UUID and string property forms
    hash identically. The authorizing principal and reference are bound
    into the digest: the same key + request from a different principal,
    or under a different reference, is a different authorization and
    conflicts instead of replaying.
    """
    if not isinstance(request, dict):
        raise TypeError("request must be a dict of immutable request details")
    for key in request:
        if not isinstance(key, str):
            raise TypeError("request keys must be strings")
    agent_value = agent.value if isinstance(agent, AgentId) else str(agent)
    canonical = {
        "action_type": action_type,
        "agent": agent_value,
        "property_id": None if property_id is None else str(property_id),
        "request": request,
        "authorized_by": authorized_by,
        "authorization_reference": authorization_reference,
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _require_text(value: Any, name: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > max_chars:
        raise ValueError(f"{name} exceeds {max_chars} characters")
    return value


def _coerce_property_id(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid property_id: {value!r}") from exc


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


def record_authorization(
    *,
    idempotency_key: str,
    action_type: str,
    agent: AgentId | str,
    request: dict[str, Any],
    authorized_by: str,
    authorization_reference: str,
    property_id: UUID | str | None = None,
    config: DatabaseConfig | None = None,
) -> AuthorizationResult:
    """Record one external-action authorization (internal interface).

    On a new key: inserts the AUTHORIZED action row and its permanent
    audit event in ONE PostgreSQL transaction and returns
    ``created=True``. On key replay with identical request,
    principal, and reference: returns the existing action with
    ``created=False`` and writes no duplicate audit event. On key
    reuse with a different request, principal, or reference:
    raises :class:`IdempotencyConflictError`.

    All validation (identities, references, fingerprint, audit event)
    completes BEFORE any connection opens. If PostgreSQL is
    unavailable the call fails closed -- no SQLite authorization
    fallback exists, by design. The ``authorized_by`` principal is
    recorded as CLAIMED (see module docstring): this function must
    not be exposed through an API route until verified principal
    binding exists.
    """
    key = _require_text(idempotency_key, "idempotency_key", MAX_IDEMPOTENCY_KEY_CHARS)
    kind = _require_text(action_type, "action_type", MAX_ACTION_TYPE_CHARS)
    principal = _require_text(authorized_by, "authorized_by", MAX_PRINCIPAL_CHARS)
    reference = _require_text(
        authorization_reference, "authorization_reference", MAX_REFERENCE_CHARS
    )
    try:
        agent_id = agent if isinstance(agent, AgentId) else AgentId(str(agent))
    except ValueError as exc:
        raise ValueError(f"unknown agent: {agent!r}") from exc
    prop_id = _coerce_property_id(property_id)
    fingerprint = canonical_request_fingerprint(
        kind, agent_id, prop_id, request, principal, reference
    )
    action_id = uuid4()
    audit = AgentEvent(
        agent=agent_id,
        run_id=str(action_id),
        event_type=_AUDIT_EVENT_TYPE,
        summary=f"Authorized {kind} action {action_id} by {principal}",
        severity=Severity.INFO,
        retention_class=RetentionClass.PERMANENT,
        details={
            "action_id": str(action_id),
            "idempotency_key": key,
            "action_type": kind,
            "request_fingerprint": fingerprint,
            "authorized_by": principal,
            "authorization_reference": reference,
            "state": ActionState.AUTHORIZED.value,
        },
        correlation_id=key,
        property_id=prop_id,
    )
    conn = get_connection(config)  # raises before any ledger effect
    row: dict[str, Any] | None = None
    existing: dict[str, Any] | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO external_actions (action_id, idempotency_key, action_type,"
                " agent, property_id, request_fingerprint, authorized_by,"
                " authorization_reference, authorized_at, state)"
                " VALUES (%(action_id)s, %(idempotency_key)s, %(action_type)s,"
                " %(agent)s, %(property_id)s, %(request_fingerprint)s,"
                " %(authorized_by)s, %(authorization_reference)s, now(), 'AUTHORIZED')"
                " ON CONFLICT (idempotency_key) DO NOTHING"
                f" RETURNING {_SELECT_COLUMNS}",
                {
                    "action_id": action_id,
                    "idempotency_key": key,
                    "action_type": kind,
                    "agent": agent_id.value,
                    "property_id": prop_id,
                    "request_fingerprint": fingerprint,
                    "authorized_by": principal,
                    "authorization_reference": reference,
                },
            )
            row = cur.fetchone()
            if row is None:
                # Key already exists: fetch the winner for comparison.
                cur.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM external_actions"
                    " WHERE idempotency_key = %(idempotency_key)s",
                    {"idempotency_key": key},
                )
                existing = cur.fetchone()
        if row is not None:
            action = dict(row)
            insert_event_in_transaction(conn, audit)
            conn.commit()
            _quiet_close(conn)
            return AuthorizationResult(action=action, created=True)
    except (DatabaseNotConfiguredError, DatabaseUnavailableError):
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise
    except Exception as exc:
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise DatabaseUnavailableError("authorization_failed") from exc
    # Replay path (no driver failure so far): nothing of ours to commit.
    _quiet_rollback(conn)
    _quiet_close(conn)
    if existing is None:
        # Winner has not committed yet: genuinely ambiguous, retryable
        # once the race resolves.
        raise DatabaseUnavailableError("authorization_race")
    if existing["request_fingerprint"] != fingerprint:
        raise IdempotencyConflictError(
            f"idempotency key {key!r} was reused for a different request"
        )
    return AuthorizationResult(action=dict(existing), created=False)
