"""Consequential external-action ledger (MVP-004C-A Phase 3C-3A).

PostgreSQL-backed authorization records for consequential external
actions (outreach, contracts, external instructions). This module
records durable authorization and transaction-safe lifecycle
advancement. It never executes external actions, runs workers, or
performs network I/O; execution belongs to a future phase.

Authorization vs execution: a row in ``external_actions`` proves an
action was authorized at a point in time. Later states record
lifecycle decisions (queued, claimed, outcome recorded), never
external side effects performed by this module.

Lifecycle safety (Phase 3C-3B-1, rule GRW-EXT-001): once an action
reaches ATTEMPTING, nothing here re-executes it automatically.
Enforced in code, not just documentation: ATTEMPTING exits only via
recorded outcomes (SUCCEEDED/FAILED/UNKNOWN), UNKNOWN has no
executable exits in this phase, FAILED has no requeue path, and
PENDING claims exactly one winner per action. Reconciliation of
UNKNOWN outcomes and retry of FAILED actions are deferred until
verified identity and evidence infrastructure exist (3C-3B-2).

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

Verified identity (Phase 3C-3B-2A): ``authorized_by`` and
``executor_id`` are server-resolved from verification, never echoed
from caller claims. Human authorization requires an Ed25519 approval
signature over the exact approval, verified against enrolled
operator public keys; executor claims require the enrolled service
credential. Forged names, constructed principal objects, and bare
strings confer nothing -- the ledger verifies proofs itself before
any database mutation. Authentication (who) and authorization policy
(may a human authorize / may an executor claim) are explicit,
separate steps in :mod:`app.identity`.

Scoped enqueue, scoped claim, verified outcomes (Phase 3C-3B-2B):
AUTHORIZED -> PENDING requires a verified orchestrator service
with an exact-match scope grant for the stored action type;
claims require the verified executor to hold a grant for the
claimed row's type (denial rolls the conditional UPDATE back, so
the action stays PENDING with no attempt and no audit); outcomes
require the claiming executor's credentials plus exact ownership
of the open attempt. Orchestrator and executor credentials live
in separate namespaces and neither verifies as the other; a
secret value enrolled in both is rejected before either role
can authenticate. There is no human break-glass or override
path: if credentials are
revoked while an attempt is unresolved, the attempt stays
unresolved for future human reconciliation -- never reassigned,
replayed, or auto-resolved.

LIMITATIONS (read before exposing this): this interface is
internal-only and must not be exposed through an API route. Human
private keys never enter this process, so human approvals are
unforgeable even in-process; executor and orchestrator service
secrets live in the process environment (like all service secrets
here), so service verification holds against external and
credential-less callers but not against arbitrary malicious code
inside this same process --
service-process separation is the documented future hardening (see
:mod:`app.identity`). Unpredictable per-authorization idempotency
keys (e.g. UUIDs) remain required, since key squatting can still
force a victim's submission to conflict.

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
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    canonical_identity_name,
    ensure_may_authorize,
    ensure_may_claim,
    ensure_may_claim_type,
    ensure_may_enqueue,
    ensure_may_enqueue_type,
    verify_executor,
    verify_human_approval,
    verify_orchestrator,
)
from app.observability.events import AgentEvent, AgentId, RetentionClass, Severity
from app.observability.store import insert_event_in_transaction
from app.properties_db import get_connection

# Explicit ledger field bounds: fail closed on oversized input rather
# than truncating audit identity (keys feed the audit correlation ID).
MAX_IDEMPOTENCY_KEY_CHARS = 128
MAX_ACTION_TYPE_CHARS = 128
MAX_PRINCIPAL_CHARS = 200
MAX_REFERENCE_CHARS = 200
MAX_EXECUTOR_CHARS = 200
MAX_ORCHESTRATOR_CHARS = 200

_AUDIT_EVENT_TYPE = "external_action.authorized"
_AUDIT_CLAIMED_TYPE = "external_action.claimed"
_AUDIT_TRANSITIONED_TYPE = "external_action.transitioned"

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

_ATTEMPT_COLUMNS = (
    "attempt_id",
    "action_id",
    "attempt_number",
    "executor_id",
    "claimed_at",
    "outcome",
    "outcome_recorded_at",
    "created_at",
    "updated_at",
)
_ATTEMPT_SELECT = ", ".join(_ATTEMPT_COLUMNS)


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

    Unknown states and unlisted transitions raise ValueError. Three
    policy-listed transitions are withheld by the 3C-3B-1 executing
    layer: PENDING -> FAILED (FAILED records an execution-attempt
    outcome; pre-execution cancellation needs its own protocol),
    UNKNOWN -> SUCCEEDED/FAILED (require independently verified
    reconciliation evidence, never a re-attempt), and FAILED ->
    PENDING (requires an explicit retry protocol with retry
    authorization plus proof of definitive failure).
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
    authorization_signature: str,
    authorization_issued_at: datetime,
    authorization_expires_at: datetime,
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

    Identity is verified, not claimed: ``authorized_by`` is only the
    claimed handle -- the Ed25519 ``authorization_signature`` over
    this exact approval (including its ``authorization_issued_at`` /
    ``authorization_expires_at`` lifetime) must verify against an
    enrolled authorizer public key, and the STORED principal is the
    server-resolved canonical name. Constructing a principal object
    or supplying a bare name confers nothing; verification happens
    here, before any connection opens. Authentication or policy
    denial raises before any ledger or audit mutation. This function
    must not be exposed through an API route (see module docstring).
    """
    key = _require_text(idempotency_key, "idempotency_key", MAX_IDEMPOTENCY_KEY_CHARS)
    kind = _require_text(action_type, "action_type", MAX_ACTION_TYPE_CHARS)
    _require_text(authorized_by, "authorized_by", MAX_PRINCIPAL_CHARS)
    reference = _require_text(
        authorization_reference, "authorization_reference", MAX_REFERENCE_CHARS
    )
    try:
        agent_id = agent if isinstance(agent, AgentId) else AgentId(str(agent))
    except ValueError as exc:
        raise ValueError(f"unknown agent: {agent!r}") from exc
    prop_id = _coerce_property_id(property_id)
    claimed_name = canonical_identity_name(authorized_by)
    fingerprint = canonical_request_fingerprint(
        kind, agent_id, prop_id, request, claimed_name, reference
    )
    verified = verify_human_approval(
        claimed_name=claimed_name,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        authorization_reference=reference,
        issued_at=authorization_issued_at,
        expires_at=authorization_expires_at,
        signature_hex=authorization_signature,
    )
    ensure_may_authorize(verified)
    principal = verified.name
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
            "principal_type": "human",
            "auth_method": verified.method,
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
    except (
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
    ):
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


# ---------------------------------------------------------------------------
# Transaction-safe lifecycle (Phase 3C-3B-1, rule GRW-EXT-001).
#
# claim_action and transition_action advance ledger state with a
# permanent audit event in the same PostgreSQL transaction. They never
# perform external side effects. UNKNOWN reconciliation and FAILED
# retry are policy-listed but deliberately unexecutable here
# (DeferredTransitionError); see the module docstring.
# ---------------------------------------------------------------------------


class ActionNotFoundError(RuntimeError):
    """No ledger action exists for the supplied action ID."""


class StaleAttemptError(ValueError):
    """An attempt ID is unknown, mismatched, superseded, or already closed."""


class DeferredTransitionError(RuntimeError):
    """A policy-listed transition withheld until later-phase safeguards exist."""


@dataclass(frozen=True)
class ClaimResult:
    """Outcome of one claim attempt (claimed vs lost contention)."""

    action: dict[str, Any] | None
    attempt: dict[str, Any] | None
    claimed: bool


def _coerce_uuid(value: UUID | str, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def _lifecycle_audit(
    action: dict[str, Any],
    event_type: str,
    summary: str,
    details: dict[str, Any],
) -> AgentEvent:
    """Build a permanent lifecycle audit event from a locked ledger row."""
    return AgentEvent(
        agent=AgentId(action["agent"]),
        run_id=str(action["action_id"]),
        event_type=event_type,
        summary=summary,
        severity=Severity.INFO,
        retention_class=RetentionClass.PERMANENT,
        details=details,
        correlation_id=action["idempotency_key"],
        property_id=action["property_id"],
    )


def claim_action(
    *,
    action_id: UUID | str,
    executor_id: str,
    executor_proof: str,
    config: DatabaseConfig | None = None,
) -> ClaimResult:
    """Atomically claim one PENDING action (PENDING -> ATTEMPTING).

    The claim is a single conditional UPDATE, so concurrent workers
    serialize on the row: exactly one sees PENDING and wins; losers
    get ``claimed=False`` and no authorization to proceed. The win
    path inserts the execution-attempt row (numbered from the
    atomically incremented attempt_count) and a permanent claim audit
    event in the same transaction.

    A claim authorizes proceeding to a FUTURE execution phase. It
    performs no external request. ``executor_id`` is only the claimed
    handle -- ``executor_proof`` must verify against the enrolled
    executor credential, and the STORED id is the server-resolved
    canonical value. Verification runs before any connection opens;
    denial raises before any ledger or audit mutation. The verified
    executor must also hold an exact-match scope grant for the
    row's stored action type, checked after the conditional UPDATE
    wins and before the attempt row: a scope denial rolls the
    claim back completely, leaving PENDING with no attempt and no
    audit. Lost claims write nothing and raise nothing.
    """
    aid = _coerce_uuid(action_id, "action_id")
    _require_text(executor_id, "executor_id", MAX_EXECUTOR_CHARS)
    claimed_id = canonical_identity_name(executor_id)
    verified = verify_executor(executor_id=claimed_id, proof=executor_proof)
    ensure_may_claim(verified)
    executor = verified.executor_id
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE external_actions SET state = 'ATTEMPTING',"
                " attempt_count = attempt_count + 1, updated_at = now()"
                " WHERE action_id = %(action_id)s AND state = 'PENDING'"
                f" RETURNING {_SELECT_COLUMNS}",
                {"action_id": aid},
            )
            row = cur.fetchone()
            current = None
            if row is None:
                cur.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM external_actions"
                    " WHERE action_id = %(action_id)s",
                    {"action_id": aid},
                )
                current = cur.fetchone()
        if row is None:
            _quiet_rollback(conn)
            _quiet_close(conn)
            return ClaimResult(
                action=dict(current) if current is not None else None,
                attempt=None,
                claimed=False,
            )
        action = dict(row)
        # Scope gate sits AFTER the winning UPDATE and BEFORE the
        # attempt row: denial raises AuthorizationDeniedError, the
        # handler below rolls back, and real PostgreSQL restores
        # PENDING with the original attempt_count.
        ensure_may_claim_type(verified, action["action_type"])
        attempt_id = uuid4()
        attempt_number = action["attempt_count"]
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO external_action_attempts (attempt_id, action_id,"
                " attempt_number, executor_id, claimed_at)"
                " VALUES (%(attempt_id)s, %(action_id)s, %(attempt_number)s,"
                " %(executor_id)s, now())"
                f" RETURNING {_ATTEMPT_SELECT}",
                {
                    "attempt_id": attempt_id,
                    "action_id": aid,
                    "attempt_number": attempt_number,
                    "executor_id": executor,
                },
            )
            attempt = dict(cur.fetchone())
        audit = _lifecycle_audit(
            action,
            _AUDIT_CLAIMED_TYPE,
            f"Claimed {action['action_type']} action {aid}"
            f" (attempt {attempt_number}) by {executor}",
            {
                "action_id": str(aid),
                "action_type": action["action_type"],
                "attempt_id": str(attempt_id),
                "attempt_number": attempt_number,
                "executor_id": executor,
                "from_state": ActionState.PENDING.value,
                "to_state": ActionState.ATTEMPTING.value,
                "principal_type": "executor",
                "auth_method": verified.method,
            },
        )
        insert_event_in_transaction(conn, audit)
        conn.commit()
    except (
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
        ValueError,
        TypeError,
    ):
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise
    except Exception as exc:
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise DatabaseUnavailableError("claim_failed") from exc
    _quiet_close(conn)
    return ClaimResult(action=action, attempt=attempt, claimed=True)


def _reject_deferred(current: ActionState, target: ActionState) -> None:
    """Withhold policy-listed transitions deferred past Phase 3C-3B-1."""
    if current is ActionState.PENDING and target is ActionState.ATTEMPTING:
        raise ValueError("PENDING -> ATTEMPTING requires claim_action (attempt creation)")
    if current is ActionState.PENDING and target is ActionState.FAILED:
        raise DeferredTransitionError(
            "PENDING -> FAILED is withheld: FAILED records an execution-attempt"
            " outcome, and pre-execution cancellation/rejection requires a"
            " separately designed protocol (deferred)"
        )
    if current is ActionState.UNKNOWN:
        raise DeferredTransitionError(
            "UNKNOWN reconciliation requires independently verified evidence"
            " (deferred to 3C-3B-2)"
        )
    if current is ActionState.FAILED and target is ActionState.PENDING:
        raise DeferredTransitionError(
            "FAILED retry requires an explicit retry protocol with retry"
            " authorization and proof of definitive failure (deferred)"
        )


def _load_open_attempt(
    conn: Any, action_id: UUID, attempt_id: UUID, latest_number: int
) -> dict[str, Any]:
    """Fetch the attempt row, rejecting stale/mismatched/closed attempts."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_ATTEMPT_SELECT} FROM external_action_attempts"
            " WHERE attempt_id = %(attempt_id)s FOR UPDATE",
            {"attempt_id": attempt_id},
        )
        attempt = cur.fetchone()
    if attempt is None or attempt["action_id"] != action_id:
        raise StaleAttemptError(f"attempt {attempt_id} does not belong to action {action_id}")
    if attempt["outcome"] is not None:
        raise StaleAttemptError(f"attempt {attempt_id} already recorded {attempt['outcome']}")
    if attempt["attempt_number"] != latest_number:
        raise StaleAttemptError(
            f"attempt {attempt_id} is superseded (latest is #{latest_number})"
        )
    return dict(attempt)


def transition_action(
    *,
    action_id: UUID | str,
    to_state: ActionState | str,
    attempt_id: UUID | str | None = None,
    orchestrator_id: str | None = None,
    orchestrator_proof: str | None = None,
    executor_id: str | None = None,
    executor_proof: str | None = None,
    config: DatabaseConfig | None = None,
) -> dict[str, Any]:
    """Transition one action between permitted states, atomically audited.

    Locks the action row, validates the policy transition, applies the
    state change with its permanent audit event in one transaction,
    and returns the updated row. ATTEMPTING exits require the
    currently open attempt ID and record the outcome on that attempt
    row; stale, mismatched, or already-closed attempts raise
    :class:`StaleAttemptError`. A timeout is never interpreted here:
    callers record the outcome they actually observed, including
    UNKNOWN, and UNKNOWN is left for later reconciliation.

    Every transition requires an authenticated caller, verified
    before any connection opens: orchestrator credentials for
    AUTHORIZED -> PENDING (plus an exact-match scope grant for the
    stored action type), the claiming executor's credentials for
    ATTEMPTING outcomes (plus exact ownership of the open
    attempt). Missing credentials fail before any database access;
    wrong-role, unscoped, or non-owning callers are denied without
    mutation. There is no human break-glass or administrative
    override in this phase.

    Executable in this phase: AUTHORIZED -> PENDING and ATTEMPTING
    -> SUCCEEDED/FAILED/UNKNOWN. PENDING -> ATTEMPTING must go
    through :func:`claim_action`. PENDING -> FAILED, UNKNOWN exits,
    and FAILED -> PENDING raise :class:`DeferredTransitionError`:
    FAILED records an execution-attempt outcome, and pre-execution
    cancellation, reconciliation, and retry each need a separately
    designed protocol.
    """
    aid = _coerce_uuid(action_id, "action_id")
    try:
        target = to_state if isinstance(to_state, ActionState) else ActionState(to_state)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid target state: {to_state!r}") from exc
    att_id = _coerce_uuid(attempt_id, "attempt_id") if attempt_id is not None else None
    if (orchestrator_id is None) != (orchestrator_proof is None):
        raise ValueError("orchestrator_id and orchestrator_proof must be supplied together")
    if (executor_id is None) != (executor_proof is None):
        raise ValueError("executor_id and executor_proof must be supplied together")
    if orchestrator_id is None and executor_id is None:
        raise AuthenticationFailedError("transition requires an authenticated caller")
    orchestrator = None
    if orchestrator_id is not None:
        _require_text(orchestrator_id, "orchestrator_id", MAX_ORCHESTRATOR_CHARS)
        orchestrator = verify_orchestrator(
            orchestrator_id=canonical_identity_name(orchestrator_id),
            proof=orchestrator_proof,  # type: ignore[arg-type] -- paired above
        )
    executor = None
    if executor_id is not None:
        _require_text(executor_id, "executor_id", MAX_EXECUTOR_CHARS)
        executor = verify_executor(
            executor_id=canonical_identity_name(executor_id),
            proof=executor_proof,  # type: ignore[arg-type] -- paired above
        )
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_COLUMNS} FROM external_actions"
                " WHERE action_id = %(action_id)s FOR UPDATE",
                {"action_id": aid},
            )
            row = cur.fetchone()
        if row is None:
            raise ActionNotFoundError(f"unknown action_id: {aid}")
        current = ActionState(row["state"])
        validate_transition(current, target)
        _reject_deferred(current, target)
        actor_details: dict[str, Any] = {}
        if current is ActionState.AUTHORIZED:
            # Enqueue: the only AUTHORIZED exit. Scope is checked
            # against the actual stored type from the locked row;
            # denial raises before anything is written.
            ensure_may_enqueue(orchestrator)
            ensure_may_enqueue_type(orchestrator, row["action_type"])
            actor_details = {
                "enqueued_by": orchestrator.orchestrator_id,
                "principal_type": "orchestrator",
                "auth_method": orchestrator.method,
            }
        attempt_number: int | None = None
        if current is ActionState.ATTEMPTING:
            if executor is None:
                raise AuthorizationDeniedError(
                    "recording an outcome requires the claiming executor's credentials"
                )
            if att_id is None:
                raise ValueError("attempt_id is required to record an ATTEMPTING outcome")
            attempt = _load_open_attempt(conn, aid, att_id, row["attempt_count"])
            if attempt["executor_id"] != executor.executor_id:
                raise AuthorizationDeniedError(
                    "only the claiming executor may record this attempt's outcome"
                )
            actor_details = {
                "recorded_by": executor.executor_id,
                "principal_type": "executor",
                "auth_method": executor.method,
            }
            attempt_number = attempt["attempt_number"]
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE external_action_attempts SET outcome = %(outcome)s,"
                    " outcome_recorded_at = now(), updated_at = now()"
                    " WHERE attempt_id = %(attempt_id)s",
                    {"outcome": target.value, "attempt_id": att_id},
                )
        elif att_id is not None:
            raise ValueError("attempt_id applies only to ATTEMPTING outcomes")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE external_actions SET state = %(state)s, updated_at = now()"
                " WHERE action_id = %(action_id)s"
                f" RETURNING {_SELECT_COLUMNS}",
                {"state": target.value, "action_id": aid},
            )
            updated = dict(cur.fetchone())
        audit = _lifecycle_audit(
            updated,
            _AUDIT_TRANSITIONED_TYPE,
            f"Transitioned {updated['action_type']} action {aid}"
            f" from {current.value} to {target.value}",
            {
                "action_id": str(aid),
                "from_state": current.value,
                "to_state": target.value,
                "attempt_id": str(att_id) if att_id is not None else None,
                "attempt_number": attempt_number,
                **actor_details,
            },
        )
        insert_event_in_transaction(conn, audit)
        conn.commit()
    except (
        ActionNotFoundError,
        StaleAttemptError,
        DeferredTransitionError,
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
        ValueError,
        TypeError,
    ):
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise
    except Exception as exc:
        _quiet_rollback(conn)
        _quiet_close(conn)
        raise DatabaseUnavailableError("transition_failed") from exc
    _quiet_close(conn)
    return updated
