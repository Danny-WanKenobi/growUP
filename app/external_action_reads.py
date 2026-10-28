"""Read-only external-action inspection (MVP-004C-A Phase 3C-3B-3A).

Internal recovery-foundation reads over the existing ledger tables.
Every function here issues SELECT statements only: this module never
commits, never inserts audit events, never transitions state, and
never executes external actions. It is deliberately separate from
the ledger mutation module (which it does not import): recovery
reads must never share a code path with state changes, and no
caller may treat a returned record as authorization to resume or
repeat external execution (GRW-EXT-001).

Three read-access levels, enforced before any data returns:

- WONDRUE (system-wide): the orchestrator id named by the
  Wondrue reader designation whose credential verifies may
  inspect actions of any type. Read-only by design; no state
  authority. The designation names WHO the reader is, never
  WHAT it may change.
- ORCHESTRATORS (scoped): other verified orchestrators may
  inspect only actions whose stored type their scope-map grants
  cover (exact match, reusing the enqueue scope map). Empty or
  missing scopes see nothing -- never everything.
- EXECUTORS (own attempts): verified executors may inspect only
  attempts owned by their authenticated id. They cannot enumerate
  other executors' attempts, and an arbitrary action ID reveals
  nothing without an owned attempt on it.

Staleness (detection only): an ATTEMPTING action whose open
attempt was claimed longer ago than the threshold "may require
investigation" -- nothing more. Staleness never implies failure,
never authorizes a retry, and never moves state. The default
threshold is 15 minutes (900 seconds), overridable per call.

Internal-only: no routes reference this module (pinned by test).
Authorization is enforced inside the SQL statements themselves, so
a caller cannot distinguish "absent" from "unauthorized": both
read as None / empty list. There are no separate existence probes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from app.config import DatabaseConfig
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.identity import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    ExecutorPrincipal,
    OrchestratorPrincipal,
    canonical_identity_name,
    ensure_may_claim,
    load_orchestrator_scopes,
    load_wondrue_reader_id,
    may_inspect_systemwide,
    verify_executor,
    verify_orchestrator,
)
from app.properties_db import get_connection

# Read result bounds: every listing is explicitly limited.
DEFAULT_READ_LIMIT = 50
MAX_READ_LIMIT = 200

# Staleness detection default: ATTEMPTING longer than this may
# require investigation. Informational only -- see module docstring.
DEFAULT_STALE_AFTER_SECONDS = 900

MAX_CALLER_CHARS = 200

# Explicit ledger column mirrors (this module does not import the
# mutation module, so the lists are restated; SELECT-only shape).
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
_SELECT_ACTIONS = ", ".join(f"a.{name}" for name in _ACTION_COLUMNS)

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
_SELECT_ATTEMPTS = ", ".join(f"t.{name}" for name in _ATTEMPT_COLUMNS)


def _coerce_uuid(value: UUID | str, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


def _quiet_close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass  # cleanup must not mask the real outcome


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > MAX_CALLER_CHARS:
        raise ValueError(f"{name} exceeds {MAX_CALLER_CHARS} characters")
    return value


def _require_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be positive")
    return min(limit, MAX_READ_LIMIT)


def _require_seconds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("stale_after_seconds must be an integer")
    if value < 0:
        raise ValueError("stale_after_seconds must be non-negative")
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_moment(now: datetime | None) -> datetime:
    moment = _utcnow() if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return moment.astimezone(timezone.utc)


def _verify_caller(
    *,
    orchestrator_id: str | None,
    orchestrator_proof: str | None,
    executor_id: str | None,
    executor_proof: str | None,
) -> tuple[OrchestratorPrincipal | None, ExecutorPrincipal | None]:
    """Authenticate exactly one caller role, before any database access.

    Reads have a single caller (unlike transitions, where each branch
    consumes its own role), so supplying both pairs or a half pair is
    a caller shape error, and supplying neither fails closed.
    """
    if (orchestrator_id is None) != (orchestrator_proof is None):
        raise ValueError("orchestrator_id and orchestrator_proof must be supplied together")
    if (executor_id is None) != (executor_proof is None):
        raise ValueError("executor_id and executor_proof must be supplied together")
    if orchestrator_id is None and executor_id is None:
        raise AuthenticationFailedError("inspection requires an authenticated caller")
    if orchestrator_id is not None and executor_id is not None:
        raise ValueError("supply exactly one caller credential pair")
    orchestrator = None
    if orchestrator_id is not None:
        _require_text(orchestrator_id, "orchestrator_id")
        orchestrator = verify_orchestrator(
            orchestrator_id=canonical_identity_name(orchestrator_id),
            proof=orchestrator_proof,  # type: ignore[arg-type] -- paired above
        )
    executor = None
    if executor_id is not None:
        _require_text(executor_id, "executor_id")
        executor = verify_executor(
            executor_id=canonical_identity_name(executor_id),
            proof=executor_proof,  # type: ignore[arg-type] -- paired above
        )
        ensure_may_claim(executor)
    return orchestrator, executor


def _orchestrator_type_filter(
    orchestrator: OrchestratorPrincipal,
) -> frozenset[str] | None:
    """Type filter for one verified orchestrator: None when the
    designated Wondrue reader reads unfiltered, otherwise the exact
    grant set (possibly empty, which callers short-circuit to an
    empty result without querying). Filtering inside SQL keeps
    unauthorized rows indistinguishable from missing rows.
    """
    if may_inspect_systemwide(orchestrator, load_wondrue_reader_id()):
        return None
    return load_orchestrator_scopes().get(
        orchestrator.orchestrator_id, frozenset()
    )


def _resolve_scan_scope(
    *, orchestrator_id: str, orchestrator_proof: str
) -> tuple[OrchestratorPrincipal, frozenset[str] | None]:
    """Verify one orchestrator for scan operations (no database yet).

    Returns the principal plus its type filter (None for the
    Wondrue reader, otherwise the exact grant set). Executor
    credentials fail here: scans are never executor-visible.
    """
    _require_text(orchestrator_id, "orchestrator_id")
    principal = verify_orchestrator(
        orchestrator_id=canonical_identity_name(orchestrator_id),
        proof=orchestrator_proof,
    )
    return principal, _orchestrator_type_filter(principal)


def get_action(
    *,
    action_id: UUID | str,
    orchestrator_id: str | None = None,
    orchestrator_proof: str | None = None,
    executor_id: str | None = None,
    executor_proof: str | None = None,
    config: DatabaseConfig | None = None,
) -> dict[str, Any] | None:
    """Return one action row, or None when it is unknown or unauthorized.

    Orchestrators see rows their scopes grant (the designated
    Wondrue reader sees any row); executors see a row only when
    they own at least one attempt on it. Scope and ownership are
    predicates inside the single SELECT, so unauthorized rows are
    indistinguishable from missing rows. SELECT-only; no audit
    writes, no state changes.
    """
    aid = _coerce_uuid(action_id, "action_id")
    orchestrator, executor = _verify_caller(
        orchestrator_id=orchestrator_id,
        orchestrator_proof=orchestrator_proof,
        executor_id=executor_id,
        executor_proof=executor_proof,
    )
    params: dict[str, Any] = {"action_id": aid}
    auth_clause = ""
    if orchestrator is not None:
        grants = _orchestrator_type_filter(orchestrator)
        if grants is not None and not grants:
            return None  # empty grants: nothing visible, no query issued
        if grants is not None:
            auth_clause = " AND a.action_type = ANY(%(types)s)"
            params["types"] = sorted(grants)
    else:
        assert executor is not None  # _verify_caller guarantees one role
        auth_clause = (
            " AND EXISTS (SELECT 1 FROM external_action_attempts t"
            " WHERE t.action_id = %(action_id)s"
            " AND t.executor_id = %(executor_id)s)"
        )
        params["executor_id"] = executor.executor_id
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_ACTIONS} FROM external_actions a"
                " WHERE a.action_id = %(action_id)s" + auth_clause,
                params,
            )
            row = cur.fetchone()
        return dict(row) if row is not None else None
    except (
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
        ValueError,
        TypeError,
    ):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("action_read_failed") from exc
    finally:
        _quiet_close(conn)


def list_attempts(
    *,
    action_id: UUID | str,
    orchestrator_id: str | None = None,
    orchestrator_proof: str | None = None,
    executor_id: str | None = None,
    executor_proof: str | None = None,
    limit: int = DEFAULT_READ_LIMIT,
    config: DatabaseConfig | None = None,
) -> list[dict[str, Any]]:
    """Return attempts for one action, oldest attempt number first.

    Orchestrators are scope-gated on the action's stored type via a
    JOIN predicate (the designated Wondrue reader reads unfiltered);
    executors receive only their own attempts via a SQL-side owner
    filter. Unauthorized actions read exactly like unknown actions:
    []. LIMIT applies after authorization filtering, inside SQL.
    """
    aid = _coerce_uuid(action_id, "action_id")
    capped = _require_limit(limit)
    orchestrator, executor = _verify_caller(
        orchestrator_id=orchestrator_id,
        orchestrator_proof=orchestrator_proof,
        executor_id=executor_id,
        executor_proof=executor_proof,
    )
    params: dict[str, Any] = {"action_id": aid, "limit": capped}
    join_clause = ""
    auth_clause = ""
    if orchestrator is not None:
        grants = _orchestrator_type_filter(orchestrator)
        if grants is not None and not grants:
            return []  # empty grants: nothing visible, no query issued
        if grants is not None:
            join_clause = " JOIN external_actions a ON a.action_id = t.action_id"
            auth_clause = " AND a.action_type = ANY(%(types)s)"
            params["types"] = sorted(grants)
    else:
        assert executor is not None  # _verify_caller guarantees one role
        params["executor_id"] = executor.executor_id
        auth_clause = " AND t.executor_id = %(executor_id)s"
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_ATTEMPTS} FROM external_action_attempts t"
                + join_clause +
                " WHERE t.action_id = %(action_id)s" + auth_clause +
                " ORDER BY t.attempt_number ASC, t.attempt_id ASC"
                " LIMIT %(limit)s",
                params,
            )
            rows = cur.fetchall()
        return [dict(row) for row in (rows or [])]
    except (
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
        ValueError,
        TypeError,
    ):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("attempts_read_failed") from exc
    finally:
        _quiet_close(conn)


def list_own_open_attempts(
    *,
    executor_id: str,
    executor_proof: str,
    limit: int = DEFAULT_READ_LIMIT,
    config: DatabaseConfig | None = None,
) -> list[dict[str, Any]]:
    """Return the caller's own open attempts, oldest claim first.

    Executor credentials only: the owner filter is part of the SQL
    statement, so rows belonging to other executors are never
    selected. Each record carries its action's current state, type,
    and attempt count for crash-recovery context (recording a new
    outcome still requires the owning-identity transition path).
    """
    _require_text(executor_id, "executor_id")
    capped = _require_limit(limit)
    verified = verify_executor(
        executor_id=canonical_identity_name(executor_id), proof=executor_proof
    )
    ensure_may_claim(verified)
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_SELECT_ATTEMPTS},"
                " a.state AS action_state,"
                " a.action_type AS action_type,"
                " a.attempt_count AS action_attempt_count"
                " FROM external_action_attempts t"
                " JOIN external_actions a ON a.action_id = t.action_id"
                " WHERE t.executor_id = %(executor_id)s AND t.outcome IS NULL"
                " ORDER BY t.claimed_at ASC, t.attempt_id ASC"
                " LIMIT %(limit)s",
                {"executor_id": verified.executor_id, "limit": capped},
            )
            rows = cur.fetchall()
        return [dict(row) for row in (rows or [])]
    except (
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
        ValueError,
        TypeError,
    ):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("own_attempts_read_failed") from exc
    finally:
        _quiet_close(conn)


def find_stale_attempting(
    *,
    orchestrator_id: str,
    orchestrator_proof: str,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    limit: int = DEFAULT_READ_LIMIT,
    now: datetime | None = None,
    config: DatabaseConfig | None = None,
) -> list[dict[str, Any]]:
    """Return open ATTEMPTING attempts older than the threshold.

    Detection only: a stale record "may require investigation" and
    confers no authority to act on it. The designated Wondrue
    reader scans all types; other orchestrators are filtered to
    their exact scope grants (SQL-side, so LIMIT stays
    meaningful). Empty grants short-circuit to [] without
    querying. Executors cannot scan. Each record carries the
    computed age in seconds alongside its claim timestamp.
    """
    seconds = _require_seconds(stale_after_seconds)
    capped = _require_limit(limit)
    moment = _require_moment(now)
    _, grants = _resolve_scan_scope(
        orchestrator_id=orchestrator_id, orchestrator_proof=orchestrator_proof
    )
    if grants is not None and not grants:
        return []
    cutoff = moment - timedelta(seconds=seconds)
    params: dict[str, Any] = {"cutoff": cutoff, "limit": capped}
    type_clause = ""
    if grants is not None:
        type_clause = " AND a.action_type = ANY(%(types)s)"
        params["types"] = sorted(grants)
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.action_id, a.action_type, a.state, a.attempt_count,"
                " t.attempt_id, t.attempt_number, t.executor_id, t.claimed_at"
                " FROM external_actions a"
                " JOIN external_action_attempts t ON t.action_id = a.action_id"
                " WHERE a.state = 'ATTEMPTING' AND t.outcome IS NULL"
                " AND t.claimed_at < %(cutoff)s" + type_clause +
                " ORDER BY t.claimed_at ASC, t.attempt_id ASC"
                " LIMIT %(limit)s",
                params,
            )
            rows = cur.fetchall()
        found = []
        for row in rows or []:
            record = dict(row)
            record["age_seconds"] = int((moment - record["claimed_at"]).total_seconds())
            found.append(record)
        return found
    except (
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
        ValueError,
        TypeError,
    ):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("stale_scan_failed") from exc
    finally:
        _quiet_close(conn)


def stale_summary(
    *,
    orchestrator_id: str,
    orchestrator_proof: str,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
    config: DatabaseConfig | None = None,
) -> dict[str, Any]:
    """Return bounded stale-attempt counts and oldest claim age.

    Same visibility rules as :func:`find_stale_attempting` in one
    aggregate query: no rows cross the boundary, only counts and
    the oldest timestamp. Empty results report None (never zero)
    for the oldest claim and its age.
    """
    seconds = _require_seconds(stale_after_seconds)
    moment = _require_moment(now)
    _, grants = _resolve_scan_scope(
        orchestrator_id=orchestrator_id, orchestrator_proof=orchestrator_proof
    )
    if grants is not None and not grants:
        return {
            "stale_count": 0,
            "oldest_claimed_at": None,
            "oldest_age_seconds": None,
            "threshold_seconds": seconds,
            "checked_at": moment,
        }
    cutoff = moment - timedelta(seconds=seconds)
    params: dict[str, Any] = {"cutoff": cutoff}
    type_clause = ""
    if grants is not None:
        type_clause = " AND a.action_type = ANY(%(types)s)"
        params["types"] = sorted(grants)
    conn = get_connection(config)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS stale_count, min(t.claimed_at) AS oldest_claimed_at"
                " FROM external_actions a"
                " JOIN external_action_attempts t ON t.action_id = a.action_id"
                " WHERE a.state = 'ATTEMPTING' AND t.outcome IS NULL"
                " AND t.claimed_at < %(cutoff)s" + type_clause,
                params,
            )
            row = cur.fetchone()
        oldest = row["oldest_claimed_at"] if row else None
        return {
            "stale_count": row["stale_count"] if row else 0,
            "oldest_claimed_at": oldest,
            "oldest_age_seconds": (
                int((moment - oldest).total_seconds()) if oldest is not None else None
            ),
            "threshold_seconds": seconds,
            "checked_at": moment,
        }
    except (
        DatabaseNotConfiguredError,
        DatabaseUnavailableError,
        AuthenticationFailedError,
        AuthorizationDeniedError,
        ValueError,
        TypeError,
    ):
        raise
    except Exception as exc:
        raise DatabaseUnavailableError("stale_summary_failed") from exc
    finally:
        _quiet_close(conn)
