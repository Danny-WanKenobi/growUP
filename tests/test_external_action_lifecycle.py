"""External-action lifecycle tests (Phase 3C-3B-1, rule GRW-EXT-001).

Self-contained fake PostgreSQL (lock-guarded shared tables model the
claim race honestly). Gated live-PostgreSQL tests (GROWUP_PG_INTEGRATION=1,
migrations 005+006 applied) prove concurrency, rollback, and
constraints for real. No network, no execution, no routes.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

import app.external_actions_db as ledger
from app.db import DatabaseUnavailableError
from app.identity import (
    AuthenticationFailedError,
    AuthorizationDeniedError,
    canonical_identity_name,
    sign_approval,
)
from app.observability import AgentId

_T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

_TEST_OPERATORS = ("operator-alice", "operator-bob")
_TEST_CREDS: dict[str, str] = {}  # canonical name -> private hex, per-test only

# Test-only executor secrets (clearly fake, never production). Every
# executor id used below must appear here; unknown ids fail closed.
_TEST_EXECUTORS = {
    "w-1": "test-only-executor-secret-w-1-aaaabbbbcccc",
    "w-2": "test-only-executor-secret-w-2-aaaabbbbcccc",
    "w-9": "test-only-executor-secret-w-9-aaaabbbbcccc",
    "w-denied": "test-only-executor-secret-denied-aaaa",
    "worker-1": "test-only-executor-secret-worker-1-aaaa",
    "live-w-1": "test-only-executor-secret-live-w-1-aa",
    "live-w-2": "test-only-executor-secret-live-w-2-aa",
    **{f"worker-{n}": f"test-only-executor-secret-w{n:02d}-aaaaaaaa"
       for n in range(8)},
}

# Test-only orchestrator secrets (separate namespace; never valid as
# executor proofs). sched-denied is enrolled but granted no scopes.
_TEST_ORCHESTRATORS = {
    "sched-1": "test-only-orchestrator-secret-s1-aaaa",
    "sched-2": "test-only-orchestrator-secret-s2-aaaa",
    "sched-denied": "test-only-orchestrator-secret-denied",
    "live-sched-1": "test-only-orchestrator-secret-live1",
}


def _generate_keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return private.private_bytes_raw().hex(), private.public_key().public_bytes_raw().hex()


@pytest.fixture(autouse=True)
def _enrolled_test_identities(monkeypatch):
    """Enroll fresh operator keys + executor map for every test.

    Operator private keys are runtime-generated and confined to the
    calling test. ``_auth()`` signs with them and ``_claim()`` proves
    with the executor map; tests needing no enrollment delete env vars.
    """
    _TEST_CREDS.clear()
    entries = []
    for name in _TEST_OPERATORS:
        priv, pub = _generate_keypair()
        _TEST_CREDS[name] = priv
        entries.append(f"{name}:{pub}")
    monkeypatch.setenv("AUTHORIZER_PUBLIC_KEYS", ",".join(entries))
    monkeypatch.setenv(
        "EXECUTOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _TEST_EXECUTORS.items()),
    )
    monkeypatch.setenv(
        "ORCHESTRATOR_KEYS",
        ",".join(f"{i}:{s}" for i, s in _TEST_ORCHESTRATORS.items()),
    )
    # Default scopes: every enrolled service may act on the helper's
    # default action type, except the deliberately unscoped deniers.
    monkeypatch.setenv(
        "EXECUTOR_SCOPES",
        ",".join(
            f"{i}:outreach_email" for i in _TEST_EXECUTORS if i != "w-denied"
        ),
    )
    monkeypatch.setenv(
        "ORCHESTRATOR_SCOPES",
        ",".join(
            f"{i}:outreach_email" for i in _TEST_ORCHESTRATORS if i != "sched-denied"
        ),
    )
    yield
    _TEST_CREDS.clear()


def _auth(name="operator-alice", sign=True, **overrides):
    base = {
        "idempotency_key": f"key-{uuid4().hex}",
        "action_type": "outreach_email",
        "agent": AgentId.MICHAEL,
        "request": {"template": "owner_intro"},
        "authorized_by": name,
        "authorization_reference": "ticket-123",
    }
    base.update(overrides)
    issued = base.pop("issued_at", None) or datetime.now(timezone.utc)
    expires = base.pop("expires_at", None) or (issued + timedelta(hours=1))
    if sign:
        canonical = canonical_identity_name(base["authorized_by"])
        fingerprint = ledger.canonical_request_fingerprint(
            base["action_type"],
            base["agent"],
            base.get("property_id"),
            base["request"],
            canonical,
            base["authorization_reference"],
        )
        signature = sign_approval(
            private_key_hex=_TEST_CREDS[canonical],
            idempotency_key=base["idempotency_key"],
            request_fingerprint=fingerprint,
            authorized_by=canonical,
            authorization_reference=base["authorization_reference"],
            issued_at=issued,
            expires_at=expires,
        )
    else:
        signature = "00" * 64  # never inspected: shape errors precede verification
    base["authorization_signature"] = signature
    base["authorization_issued_at"] = issued
    base["authorization_expires_at"] = expires
    return base


def _claim(action_id, executor="w-1", proof=None):
    """Claim through the real API with this file's enrolled proofs."""
    return ledger.claim_action(
        action_id=action_id,
        executor_id=executor,
        executor_proof=_TEST_EXECUTORS[executor] if proof is None else proof,
    )


def _enqueue(action_id, orch="sched-1", proof=None, **kw):
    """Enqueue through the real API with this file's enrolled proofs."""
    return ledger.transition_action(
        action_id=action_id,
        to_state="PENDING",
        orchestrator_id=orch,
        orchestrator_proof=_TEST_ORCHESTRATORS[orch] if proof is None else proof,
        **kw,
    )


def _record(action_id, attempt_id, executor="w-1", proof=None, to_state="SUCCEEDED"):
    """Record an outcome through the real API with enrolled proofs."""
    return ledger.transition_action(
        action_id=action_id,
        to_state=to_state,
        attempt_id=attempt_id,
        executor_id=executor,
        executor_proof=_TEST_EXECUTORS[executor] if proof is None else proof,
    )


def _probe(action_id, to_state, attempt_id=None):
    """Probe a policy/deferred refusal with valid orchestrator credentials.

    Policy and deferral checks precede role checks, so any
    authenticated caller reaches the same refusal.
    """
    return ledger.transition_action(
        action_id=action_id,
        to_state=to_state,
        attempt_id=attempt_id,
        orchestrator_id="sched-1",
        orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
    )


class _LifeTables:
    """Shared fake database: actions by id and key, attempts by id."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_id: dict = {}
        self.by_key: dict = {}
        self.attempts: dict = {}


class _LifeConn:
    def __init__(self, tables=None):
        self.tables = tables if tables is not None else _LifeTables()
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.commit_fails = False
        self.audit_fails: Exception | None = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _LifeCursor(self)

    def commit(self):
        if self.commit_fails:
            raise RuntimeError("simulated commit failure")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def _action_row(params):
    return {
        "action_id": params["action_id"],
        "idempotency_key": params["idempotency_key"],
        "action_type": params["action_type"],
        "agent": params["agent"],
        "property_id": params["property_id"],
        "request_fingerprint": params["request_fingerprint"],
        "authorized_by": params["authorized_by"],
        "authorization_reference": params["authorization_reference"],
        "authorized_at": _T0,
        "state": "AUTHORIZED",
        "attempt_count": 0,
        "created_at": _T0,
        "updated_at": _T0,
    }


def _attempt_row(params):
    return {
        "attempt_id": params["attempt_id"],
        "action_id": params["action_id"],
        "attempt_number": params["attempt_number"],
        "executor_id": params["executor_id"],
        "claimed_at": _T0,
        "outcome": None,
        "outcome_recorded_at": None,
        "created_at": _T0,
        "updated_at": _T0,
    }


class _LifeCursor:
    def __init__(self, conn):
        self._conn = conn
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        params = dict(params or {})
        self._conn.statements.append((sql, params))
        tables = self._conn.tables
        with tables.lock:
            if sql.startswith("INSERT INTO external_actions"):
                key = params["idempotency_key"]
                if key in tables.by_key:
                    self._fetch = None
                else:
                    row = _action_row(params)
                    tables.by_key[key] = row
                    tables.by_id[row["action_id"]] = row
                    self._fetch = row
            elif sql.startswith("SELECT") and "FROM external_actions" in sql:
                if "idempotency_key" in params:
                    self._fetch = tables.by_key.get(params["idempotency_key"])
                else:
                    self._fetch = tables.by_id.get(params["action_id"])
            elif sql.startswith("UPDATE external_actions SET state = 'ATTEMPTING'"):
                row = tables.by_id.get(params["action_id"])
                if row is not None and row["state"] == "PENDING":
                    row["state"] = "ATTEMPTING"
                    row["attempt_count"] += 1
                    self._fetch = dict(row)
                else:
                    self._fetch = None
            elif sql.startswith("UPDATE external_actions SET state = "):
                row = tables.by_id[params["action_id"]]
                row["state"] = params["state"]
                self._fetch = dict(row)
            elif sql.startswith("INSERT INTO external_action_attempts"):
                row = _attempt_row(params)
                tables.attempts[row["attempt_id"]] = row
                self._fetch = dict(row)
            elif sql.startswith("SELECT") and "FROM external_action_attempts" in sql:
                found = tables.attempts.get(params["attempt_id"])
                self._fetch = dict(found) if found is not None else None
            elif sql.startswith("UPDATE external_action_attempts"):
                tables.attempts[params["attempt_id"]]["outcome"] = params["outcome"]
                tables.attempts[params["attempt_id"]]["outcome_recorded_at"] = _T0
                self._fetch = None
            elif "INTO agent_events" in sql:
                if self._conn.audit_fails is not None:
                    raise self._conn.audit_fails
                self._fetch = None
            else:  # pragma: no cover - lifecycle issues only these statements
                raise AssertionError(f"unexpected SQL: {sql[:70]}")

    def fetchone(self):
        return self._fetch


def _factory(monkeypatch, tables=None, conn=None, exc=None):
    made: list = []
    tables = tables if tables is not None else _LifeTables()

    def _make(config=None):
        made.append(config)
        if exc is not None:
            raise exc
        return conn if conn is not None else _LifeConn(tables)

    monkeypatch.setattr(ledger, "get_connection", _make)
    return tables, made


def _audits(conn):
    return [(s, p) for s, p in conn.statements if "INTO agent_events" in s]


def _setup_authorized(monkeypatch, **overrides):
    """Real authorization on a fresh fake backend; returns (tables, action)."""
    tables, _ = _factory(monkeypatch)
    result = ledger.record_authorization(**_auth(**overrides))
    return tables, result.action


# --- Enqueue and fail-fast transitions ---


def test_enqueue_authorized_to_pending(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    updated = _enqueue(action["action_id"])
    assert updated["state"] == "PENDING"
    assert updated["action_id"] == action["action_id"]
    assert (conn.commits, conn.rollbacks) == (1, 0)
    (sql, params), *_ = _audits(conn)
    assert params["event_type"] == "external_action.transitioned"
    assert params["retention_class"] == "permanent"
    details = json.loads(params["details"])
    assert (details["from_state"], details["to_state"]) == ("AUTHORIZED", "PENDING")
    assert details["attempt_id"] is None
    assert details["enqueued_by"] == "sched-1"  # verified orchestrator attributed
    assert details["principal_type"] == "orchestrator"
    assert details["auth_method"] == "orchestrator-key"


def test_pending_to_failed_deferred_to_cancellation_protocol(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    audits_before = len(_audits(conn))
    with pytest.raises(ledger.DeferredTransitionError, match="cancellation"):
        ledger.transition_action(
            action_id=action["action_id"],
            to_state="FAILED",
            orchestrator_id="sched-1",
            orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
        )
    assert tables.by_id[action["action_id"]]["state"] == "PENDING"
    assert tables.attempts == {}
    assert len(_audits(conn)) == audits_before  # no transition audit written
    assert (conn.commits, conn.rollbacks) == (1, 1)


def test_attempt_id_rejected_outside_attempting(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    with pytest.raises(ValueError, match="only to ATTEMPTING"):
        _enqueue(action["action_id"], attempt_id=uuid4())


# --- Atomic claiming ---


def test_claim_success_creates_attempt_and_audit(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    result = _claim(action["action_id"], "worker-1")
    assert result.claimed is True
    assert result.action["state"] == "ATTEMPTING"
    assert result.action["attempt_count"] == 1
    assert result.attempt["attempt_number"] == 1
    assert result.attempt["executor_id"] == "worker-1"
    assert result.attempt["outcome"] is None
    assert result.attempt["action_id"] == action["action_id"]
    (sql, params), *_ = [a for a in _audits(conn) if "claimed" in a[1]["event_type"]]
    assert params["retention_class"] == "permanent"
    details = json.loads(params["details"])
    assert details["attempt_number"] == 1
    assert details["executor_id"] == "worker-1"
    assert details["action_type"] == "outreach_email"  # scope-check evidence
    assert conn.commits == 2  # enqueue + claim on this handle


def test_claim_loses_quietly_when_not_pending(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    before = len(_audits(conn))
    lost = _claim(action["action_id"], "worker-1")
    assert lost.claimed is False
    assert lost.attempt is None
    assert lost.action["state"] == "AUTHORIZED"  # context, not authorization
    assert len(_audits(conn)) == before  # losers write nothing
    assert (conn.commits, conn.rollbacks) == (0, 1)


def test_claim_unknown_action_loses_with_no_row(monkeypatch) -> None:
    tables, _ = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    lost = _claim(uuid4(), "worker-1")
    assert (lost.claimed, lost.action, lost.attempt) == (False, None, None)


def test_repeated_claim_second_loses(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    first = _claim(action["action_id"], "w-1")
    second = _claim(action["action_id"], "w-2")
    assert (first.claimed, second.claimed) == (True, False)
    assert second.action["state"] == "ATTEMPTING"
    assert len(tables.attempts) == 1


def test_concurrent_claims_single_winner(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    results: list = []
    errors: list = []

    def _race(n):
        try:
            results.append(
                _claim(action["action_id"], f"worker-{n}")
            )
        except Exception as exc:  # pragma: no cover - must not happen
            errors.append(exc)

    threads = [threading.Thread(target=_race, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sum(1 for r in results if r.claimed) == 1
    assert len(tables.attempts) == 1
    assert tables.by_id[action["action_id"]]["attempt_count"] == 1


def test_claim_validates_before_io(monkeypatch) -> None:
    _, made = _factory(monkeypatch)
    with pytest.raises(ValueError):
        _claim("not-a-uuid", "w-1")
    with pytest.raises(ValueError):
        ledger.claim_action(
            action_id=uuid4(), executor_id="  ", executor_proof="unused-proof"
        )
    assert made == []


def test_claim_audit_failure_rolls_back(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    conn.audit_fails = DatabaseUnavailableError("event_insert_failed")
    with pytest.raises(DatabaseUnavailableError):
        _claim(action["action_id"])
    assert conn.commits == 1 and conn.rollbacks == 1  # enqueue kept, claim rolled back


def test_claim_commit_failure_reports_failure(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    conn.commit_fails = True
    with pytest.raises(DatabaseUnavailableError, match="claim_failed"):
        _claim(action["action_id"])


# --- Outcomes ---


def _setup_attempting(monkeypatch):
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    claim = _claim(action["action_id"])
    return tables, action, claim


def test_outcome_succeeded_recorded_on_attempt(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    updated = _record(action["action_id"], claim.attempt["attempt_id"])
    assert updated["state"] == "SUCCEEDED"
    attempt = tables.attempts[claim.attempt["attempt_id"]]
    assert attempt["outcome"] == "SUCCEEDED"
    assert attempt["outcome_recorded_at"] is not None
    assert attempt["executor_id"] == "w-1"  # history preserved, not rewritten
    (sql, params), *_ = _audits(conn)
    details = json.loads(params["details"])
    assert (details["from_state"], details["to_state"]) == ("ATTEMPTING", "SUCCEEDED")
    assert details["attempt_id"] == str(claim.attempt["attempt_id"])
    assert details["attempt_number"] == 1
    assert details["recorded_by"] == "w-1"  # owning executor attributed
    assert details["principal_type"] == "executor"
    assert details["auth_method"] == "executor-key"


def test_outcome_failed_and_unknown_recorded(monkeypatch) -> None:
    for outcome in ("FAILED", "UNKNOWN"):
        tables, action, claim = _setup_attempting(monkeypatch)
        updated = _record(
            action["action_id"], claim.attempt["attempt_id"], to_state=outcome
        )
        assert updated["state"] == outcome
        assert tables.attempts[claim.attempt["attempt_id"]]["outcome"] == outcome


def test_unknown_outcome_does_not_auto_transition(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    updated = _record(
        action["action_id"], claim.attempt["attempt_id"], to_state="UNKNOWN"
    )
    assert updated["state"] == "UNKNOWN"
    assert tables.by_id[action["action_id"]]["state"] == "UNKNOWN"
    with pytest.raises(ledger.DeferredTransitionError):
        ledger.transition_action(
            action_id=action["action_id"],
            to_state="SUCCEEDED",
            executor_id="w-1",
            executor_proof=_TEST_EXECUTORS["w-1"],
        )


def test_outcome_requires_open_attempt_id(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    with pytest.raises(ValueError, match="attempt_id is required"):
        ledger.transition_action(
            action_id=action["action_id"],
            to_state="SUCCEEDED",
            executor_id="w-1",
            executor_proof=_TEST_EXECUTORS["w-1"],
        )
    assert tables.by_id[action["action_id"]]["state"] == "ATTEMPTING"


def test_stale_attempt_ids_rejected(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    good = claim.attempt["attempt_id"]
    with pytest.raises(ledger.StaleAttemptError):
        _record(action["action_id"], uuid4())
    other_auth = ledger.record_authorization(**_auth())
    other_id = other_auth.action["action_id"]
    _enqueue(other_id)
    other_claim = _claim(other_id, "w-9")
    with pytest.raises(ledger.StaleAttemptError):
        _record(action["action_id"], other_claim.attempt["attempt_id"])
    _record(action["action_id"], good)
    with pytest.raises(ValueError):  # terminal state rules first
        _record(action["action_id"], good, to_state="FAILED")


def test_closed_attempt_rejected_when_state_lags_defense_in_depth(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    # Unreachable via public API (outcome flips both rows atomically);
    # the check exists so a hand-edited ledger still fails closed.
    tables.attempts[claim.attempt["attempt_id"]]["outcome"] = "SUCCEEDED"
    with pytest.raises(ledger.StaleAttemptError, match="already recorded"):
        _record(action["action_id"], claim.attempt["attempt_id"])


def test_superseded_attempt_rejected_defense_in_depth(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    # Unreachable via public API (a second claim always loses); the
    # check exists so a hand-edited ledger still fails closed.
    tables.by_id[action["action_id"]]["attempt_count"] = 2
    with pytest.raises(ledger.StaleAttemptError, match="superseded"):
        _record(action["action_id"], claim.attempt["attempt_id"])


def test_transition_audit_failure_rolls_back(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    conn.audit_fails = DatabaseUnavailableError("event_insert_failed")
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(DatabaseUnavailableError):
        _record(action["action_id"], claim.attempt["attempt_id"])
    assert (conn.commits, conn.rollbacks) == (0, 1)


# --- GRW-EXT-001: no re-execution, no requeue, no silent retry ---


def test_no_automatic_retry_after_attempting(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    lost = _claim(action["action_id"], "w-2")
    assert lost.claimed is False  # ATTEMPTING actions are never re-claimed
    assert len(tables.attempts) == 1
    with pytest.raises(ValueError):
        _probe(action["action_id"], "PENDING")


def test_no_requeue_from_unknown(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    _record(action["action_id"], claim.attempt["attempt_id"], to_state="UNKNOWN")
    with pytest.raises(ValueError):  # policy-level refusal
        _probe(action["action_id"], "PENDING")
    with pytest.raises(ValueError):
        _probe(action["action_id"], "ATTEMPTING")


def test_no_unrestricted_retry_from_failed(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    _record(action["action_id"], claim.attempt["attempt_id"], to_state="FAILED")
    with pytest.raises(ledger.DeferredTransitionError, match="retry protocol"):
        _probe(action["action_id"], "PENDING")


def test_succeeded_is_terminal(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    _record(action["action_id"], claim.attempt["attempt_id"])
    for target in ("PENDING", "ATTEMPTING", "FAILED", "UNKNOWN", "AUTHORIZED"):
        with pytest.raises(ValueError):
            _probe(action["action_id"], target)


def test_pending_to_attempting_requires_claim(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    with pytest.raises(ValueError, match="claim_action"):
        _probe(action["action_id"], "ATTEMPTING")


def test_invalid_targets_and_unknown_actions(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    with pytest.raises(ValueError, match="invalid target state"):
        ledger.transition_action(action_id=action["action_id"], to_state="EXECUTING")
    with pytest.raises(ValueError):
        _probe(action["action_id"], "SUCCEEDED")
    with pytest.raises(ledger.ActionNotFoundError):
        _probe(uuid4(), "PENDING")
    with pytest.raises(ValueError, match="invalid action_id"):
        ledger.transition_action(action_id="nope", to_state="PENDING")


# --- Full lifecycle ---


def test_full_lifecycle_four_audits(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    claim = _claim(action["action_id"])
    _record(action["action_id"], claim.attempt["attempt_id"])
    types = [p["event_type"] for _, p in _audits(conn)]
    assert types == [
        "external_action.transitioned",
        "external_action.claimed",
        "external_action.transitioned",
    ]
    keys = {p["correlation_id"] for _, p in _audits(conn)}
    assert keys == {action["idempotency_key"]}  # one lifecycle, one correlation
    assert tables.by_id[action["action_id"]]["state"] == "SUCCEEDED"


# --- Failure posture and scope guards ---


def test_postgresql_unavailable_fails_closed(monkeypatch) -> None:
    import sqlite3

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not touch SQLite")
        ),
    )
    _factory(
        monkeypatch, exc=DatabaseUnavailableError("connection_failed")
    )
    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        _claim(uuid4())
    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        _probe(uuid4(), "PENDING")  # verified creds, then the connection fails


def test_module_defines_no_execution_or_retry_surface() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/external_actions_db.py").read_text(encoding="utf-8")
    )
    defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert defs == {
        "record_authorization",
        "claim_action",
        "transition_action",
        "validate_transition",
        "canonical_request_fingerprint",
        "_require_text",
        "_coerce_property_id",
        "_quiet_rollback",
        "_quiet_close",
        "_coerce_uuid",
        "_lifecycle_audit",
        "_reject_deferred",
        "_load_open_attempt",
    }, defs
    for name in defs:
        lowered = name.lower()
        assert not any(
            stem in lowered
            for stem in ("retry", "worker", "execut", "send", "perform", "outreach",
                         "deliver", "publish", "background", "schedul", "poll", "reconcile")
        ), name


# --- Claim identity boundary (3C-3B-2A) ---


def test_claim_bad_proof_denied_before_connection(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    _, made = _factory(monkeypatch, tables=tables, conn=conn)
    _enqueue(action["action_id"])
    calls_before = len(made)
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        ledger.claim_action(
            action_id=action["action_id"],
            executor_id="w-1",
            executor_proof="wrong-proof-0000000000000000",
        )
    assert len(made) == calls_before  # denied before any connection opened
    assert tables.by_id[action["action_id"]]["state"] == "PENDING"
    assert tables.attempts == {}


def test_claim_unknown_executor_denied_without_mutation(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        ledger.claim_action(
            action_id=action["action_id"],
            executor_id="ghost-9",
            executor_proof="test-only-ghost-proof-00000000",
        )
    assert tables.by_id[action["action_id"]]["state"] == "PENDING"
    assert tables.attempts == {}


def test_claim_missing_enrollment_denied(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    monkeypatch.delenv("EXECUTOR_KEYS", raising=False)
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        _claim(action["action_id"], "w-1")
    assert tables.by_id[action["action_id"]]["state"] == "PENDING"


def test_claim_cross_role_human_signature_denied(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    args = _auth()  # a valid human approval signature is not executor proof
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        ledger.claim_action(
            action_id=action["action_id"],
            executor_id="operator-alice",
            executor_proof=args["authorization_signature"],
        )
    assert tables.by_id[action["action_id"]]["state"] == "PENDING"


def test_claim_proof_never_persisted(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    _claim(action["action_id"], "w-1")
    blob = json.dumps(conn.statements, default=str)
    assert _TEST_EXECUTORS["w-1"] not in blob


def test_claim_canonicalizes_executor(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    result = ledger.claim_action(
        action_id=action["action_id"],
        executor_id="W-1",
        executor_proof=_TEST_EXECUTORS["w-1"],
    )
    assert result.claimed is True
    assert result.attempt["executor_id"] == "w-1"  # stored canonical


# --- Scoped enqueue, scoped claim, verified outcomes (3C-3B-2B) ---


def test_enqueue_missing_credentials_denied_before_connection(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    _, made = _factory(monkeypatch, tables=tables, conn=conn)
    with pytest.raises(AuthenticationFailedError, match="authenticated caller"):
        ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    assert made == []  # denied before any connection opened
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"


def test_enqueue_half_supplied_pair_rejected(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    _, made = _factory(monkeypatch, tables=tables)
    with pytest.raises(ValueError, match="supplied together"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="PENDING",
            orchestrator_id="sched-1",
        )
    with pytest.raises(ValueError, match="supplied together"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="PENDING",
            orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
        )
    assert made == []


def test_enqueue_bad_proof_denied_before_connection(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    _, made = _factory(monkeypatch, tables=tables, conn=conn)
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        _enqueue(action["action_id"], proof="wrong-proof-0000000000000000")
    assert made == []
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"
    assert tables.attempts == {}


def test_enqueue_unknown_orchestrator_denied(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    with pytest.raises(AuthenticationFailedError, match="unknown orchestrator"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="PENDING",
            orchestrator_id="ghost-9",
            orchestrator_proof="test-only-ghost-proof-00000000",
        )
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"


def test_enqueue_executor_credentials_denied_cross_role(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(AuthorizationDeniedError, match="verified orchestrator"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="PENDING",
            executor_id="w-1", executor_proof=_TEST_EXECUTORS["w-1"],
        )
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"
    assert _audits(conn) == []
    assert (conn.commits, conn.rollbacks) == (0, 1)


def test_enqueue_unscoped_orchestrator_denied_without_mutation(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(AuthorizationDeniedError, match="not permitted to enqueue"):
        _enqueue(action["action_id"], "sched-denied")
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"
    assert _audits(conn) == []
    assert (conn.commits, conn.rollbacks) == (0, 1)


def test_enqueue_wrong_type_scope_denied(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch, action_type="contract_sign")
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(AuthorizationDeniedError, match="not permitted to enqueue"):
        _enqueue(action["action_id"])  # sched-1 holds outreach_email only
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"
    assert _audits(conn) == []


def test_enqueue_missing_scopes_denied(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    monkeypatch.delenv("ORCHESTRATOR_SCOPES", raising=False)
    with pytest.raises(AuthorizationDeniedError, match="not permitted to enqueue"):
        _enqueue(action["action_id"])
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"


def test_enqueue_malformed_scopes_fail_closed(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "bogus-entry")
    with pytest.raises(AuthorizationDeniedError, match="scope map"):
        _enqueue(action["action_id"])
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"


def test_enqueue_scope_case_sensitive(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "sched-1:Outreach_Email")
    with pytest.raises(AuthorizationDeniedError, match="not permitted to enqueue"):
        _enqueue(action["action_id"])  # stored type is lowercase: no match
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"


def test_enqueue_proof_never_persisted(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    blob = json.dumps(conn.statements, default=str)
    assert _TEST_ORCHESTRATORS["sched-1"] not in blob


def test_enqueue_canonicalizes_orchestrator(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    updated = ledger.transition_action(
        action_id=action["action_id"], to_state="PENDING",
        orchestrator_id="Sched-1",
        orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
    )
    assert updated["state"] == "PENDING"
    (sql, params), *_ = _audits(conn)
    assert json.loads(params["details"])["enqueued_by"] == "sched-1"  # stored canonical


def test_claim_unscoped_executor_rolled_back(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    with pytest.raises(AuthorizationDeniedError, match="not permitted to claim"):
        _claim(action["action_id"], "w-denied")
    assert (conn.commits, conn.rollbacks) == (1, 1)  # enqueue kept, claim rolled back
    assert not [
        s for s, _ in conn.statements
        if s.startswith("INSERT INTO external_action_attempts")
    ]
    assert not [
        a for a in _audits(conn) if a[1]["event_type"] == "external_action.claimed"
    ]
    # NOTE: this fake applies the UPDATE immediately, so PENDING
    # restoration is proven by the live-PostgreSQL test below.


def test_claim_wrong_type_scope_denied(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch, action_type="contract_sign")
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    monkeypatch.setenv("ORCHESTRATOR_SCOPES", "sched-1:contract_sign")
    _enqueue(action["action_id"])
    with pytest.raises(AuthorizationDeniedError, match="not permitted to claim"):
        _claim(action["action_id"], "w-1")  # w-1 holds outreach_email only
    assert not [
        s for s, _ in conn.statements
        if s.startswith("INSERT INTO external_action_attempts")
    ]
    assert not [
        a for a in _audits(conn) if a[1]["event_type"] == "external_action.claimed"
    ]


def test_claim_scope_case_sensitive(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    monkeypatch.setenv("EXECUTOR_SCOPES", "w-1:Outreach_Email")
    with pytest.raises(AuthorizationDeniedError, match="not permitted to claim"):
        _claim(action["action_id"], "w-1")  # stored type is lowercase: no match
    assert (conn.commits, conn.rollbacks) == (1, 1)


def test_claim_missing_scopes_denied(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    monkeypatch.delenv("EXECUTOR_SCOPES", raising=False)
    with pytest.raises(AuthorizationDeniedError, match="not permitted to claim"):
        _claim(action["action_id"], "w-1")
    assert (conn.commits, conn.rollbacks) == (1, 1)


def test_claim_malformed_scopes_fail_closed(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _enqueue(action["action_id"])
    monkeypatch.setenv("EXECUTOR_SCOPES", "bogus-entry")
    with pytest.raises(AuthorizationDeniedError, match="scope map"):
        _claim(action["action_id"], "w-1")
    assert (conn.commits, conn.rollbacks) == (1, 1)


def test_claim_orchestrator_credential_denied(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    with pytest.raises(AuthenticationFailedError, match="unknown executor"):
        ledger.claim_action(
            action_id=action["action_id"],
            executor_id="sched-1",
            executor_proof=_TEST_ORCHESTRATORS["sched-1"],
        )
    assert tables.by_id[action["action_id"]]["state"] == "PENDING"
    assert tables.attempts == {}


def test_concurrent_denied_claims_create_nothing(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    _enqueue(action["action_id"])
    errors: list = []
    lost: list = []

    def _race():
        try:
            lost.append(_claim(action["action_id"], "w-denied"))
        except AuthorizationDeniedError as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_race) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(errors) == 1  # exactly one UPDATE winner, and it is denied
    assert len(lost) == 5  # every other racer loses quietly
    assert all(r.claimed is False for r in lost)
    assert tables.attempts == {}  # denials happen before the attempt row


def test_outcome_missing_credentials_denied_before_connection(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    _, made = _factory(monkeypatch, tables=tables)
    with pytest.raises(AuthenticationFailedError, match="authenticated caller"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="SUCCEEDED",
            attempt_id=claim.attempt["attempt_id"],
        )
    assert made == []  # denied before any connection opened
    assert tables.attempts[claim.attempt["attempt_id"]]["outcome"] is None


def test_outcome_bad_proof_denied_before_connection(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    _, made = _factory(monkeypatch, tables=tables)
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        _record(
            action["action_id"], claim.attempt["attempt_id"],
            proof="wrong-proof-0000000000000000",
        )
    assert made == []
    assert tables.by_id[action["action_id"]]["state"] == "ATTEMPTING"


def test_outcome_orchestrator_credentials_denied_cross_role(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(AuthorizationDeniedError, match="claiming executor"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="SUCCEEDED",
            attempt_id=claim.attempt["attempt_id"],
            orchestrator_id="sched-1",
            orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
        )
    assert tables.by_id[action["action_id"]]["state"] == "ATTEMPTING"
    assert tables.attempts[claim.attempt["attempt_id"]]["outcome"] is None
    assert _audits(conn) == []
    assert (conn.commits, conn.rollbacks) == (0, 1)


def test_outcome_non_owner_denied_without_mutation(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(AuthorizationDeniedError, match="only the claiming executor"):
        _record(action["action_id"], claim.attempt["attempt_id"], "w-2")
    assert tables.by_id[action["action_id"]]["state"] == "ATTEMPTING"
    assert tables.attempts[claim.attempt["attempt_id"]]["outcome"] is None
    assert _audits(conn) == []
    assert (conn.commits, conn.rollbacks) == (0, 1)


def test_outcome_owner_may_record_after_non_owner_denied(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    with pytest.raises(AuthorizationDeniedError):
        _record(action["action_id"], claim.attempt["attempt_id"], "w-2")
    updated = _record(action["action_id"], claim.attempt["attempt_id"], "w-1")
    assert updated["state"] == "SUCCEEDED"
    assert tables.attempts[claim.attempt["attempt_id"]]["outcome"] == "SUCCEEDED"


def test_outcome_proof_never_persisted(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    _record(action["action_id"], claim.attempt["attempt_id"])
    blob = json.dumps(conn.statements, default=str)
    assert _TEST_EXECUTORS["w-1"] not in blob


def test_claim_rejected_when_namespaces_overlap(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    _, made = _factory(monkeypatch, tables=tables, conn=conn)
    _enqueue(action["action_id"])
    calls_before = len(made)
    monkeypatch.setenv(
        "ORCHESTRATOR_KEYS",
        f"sched-9:{_TEST_EXECUTORS['w-1']}",  # same value, other role
    )
    with pytest.raises(AuthenticationFailedError, match="namespaces overlap"):
        _claim(action["action_id"], "w-1")
    assert len(made) == calls_before  # rejected before any connection opened
    assert tables.by_id[action["action_id"]]["state"] == "PENDING"
    assert tables.attempts == {}


def test_enqueue_rejected_when_namespaces_overlap(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    _, made = _factory(monkeypatch, tables=tables)
    monkeypatch.setenv(
        "EXECUTOR_KEYS", f"w-9:{_TEST_ORCHESTRATORS['sched-1']}"
    )
    with pytest.raises(AuthenticationFailedError, match="namespaces overlap"):
        _enqueue(action["action_id"])
    assert made == []
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"


def test_transition_rejects_malformed_counterpart_config(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    _, made = _factory(monkeypatch, tables=tables)
    monkeypatch.setenv("EXECUTOR_KEYS", "bogus-entry")
    with pytest.raises(AuthenticationFailedError, match="executor map"):
        _enqueue(action["action_id"])
    assert made == []
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"


def test_enqueue_with_both_roles_uses_orchestrator_only(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    updated = ledger.transition_action(
        action_id=action["action_id"], to_state="PENDING",
        orchestrator_id="sched-1",
        orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
        executor_id="w-1",
        executor_proof=_TEST_EXECUTORS["w-1"],
    )
    assert updated["state"] == "PENDING"
    (sql, params), *_ = _audits(conn)
    details = json.loads(params["details"])
    assert details["enqueued_by"] == "sched-1"
    assert "recorded_by" not in details and "executor_id" not in details


def test_enqueue_with_both_roles_unscoped_orchestrator_denied(monkeypatch) -> None:
    # A valid executor credential cannot substitute for the enqueue role.
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(AuthorizationDeniedError, match="not permitted to enqueue"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="PENDING",
            orchestrator_id="sched-denied",
            orchestrator_proof=_TEST_ORCHESTRATORS["sched-denied"],
            executor_id="w-1",
            executor_proof=_TEST_EXECUTORS["w-1"],
        )
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"
    assert _audits(conn) == []


def test_outcome_with_both_roles_uses_owner_only(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    updated = ledger.transition_action(
        action_id=action["action_id"], to_state="SUCCEEDED",
        attempt_id=claim.attempt["attempt_id"],
        orchestrator_id="sched-1",
        orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
        executor_id="w-1",
        executor_proof=_TEST_EXECUTORS["w-1"],
    )
    assert updated["state"] == "SUCCEEDED"
    (sql, params), *_ = _audits(conn)
    details = json.loads(params["details"])
    assert details["recorded_by"] == "w-1"
    assert "enqueued_by" not in details


def test_outcome_with_both_roles_non_owner_denied(monkeypatch) -> None:
    # A valid orchestrator credential cannot substitute for ownership.
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(AuthorizationDeniedError, match="only the claiming executor"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="SUCCEEDED",
            attempt_id=claim.attempt["attempt_id"],
            orchestrator_id="sched-1",
            orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
            executor_id="w-2",
            executor_proof=_TEST_EXECUTORS["w-2"],
        )
    assert tables.by_id[action["action_id"]]["state"] == "ATTEMPTING"
    assert tables.attempts[claim.attempt["attempt_id"]]["outcome"] is None
    assert _audits(conn) == []


def test_presented_but_bad_second_proof_fails_closed(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    _, made = _factory(monkeypatch, tables=tables)
    with pytest.raises(AuthenticationFailedError, match="mismatch"):
        ledger.transition_action(
            action_id=action["action_id"], to_state="PENDING",
            orchestrator_id="sched-1",
            orchestrator_proof=_TEST_ORCHESTRATORS["sched-1"],
            executor_id="w-1",
            executor_proof="wrong-proof-0000000000000000",
        )
    assert made == []  # any presented proof must verify
    assert tables.by_id[action["action_id"]]["state"] == "AUTHORIZED"


# --- Gated PostgreSQL-backed integration ---


def _live_config():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _live_cleanup(action_ids, keys):
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM external_action_attempts WHERE action_id = ANY(%(ids)s)",
                {"ids": list(action_ids)},
            )
            cur.execute(
                "DELETE FROM external_actions WHERE action_id = ANY(%(ids)s)",
                {"ids": list(action_ids)},
            )
            cur.execute(
                "DELETE FROM agent_events WHERE correlation_id = ANY(%(ids)s)",
                {"ids": list(keys)},
            )
        conn.commit()


@needs_live_pg
def test_live_full_lifecycle_to_succeeded() -> None:
    _live_config()
    key = f"live-3cb1-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        assert _enqueue(action_id, "live-sched-1")["state"] == "PENDING"
        claim = _claim(action_id, "live-w-1")
        assert claim.claimed is True
        assert claim.attempt["attempt_number"] == 1
        assert _record(
            action_id, claim.attempt["attempt_id"], "live-w-1"
        )["state"] == "SUCCEEDED"
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM agent_events WHERE correlation_id = %(key)s",
                    {"key": key},
                )
                assert cur.fetchone()["n"] == 4  # auth + enqueue + claim + outcome
                cur.execute(
                    "SELECT outcome, executor_id FROM external_action_attempts"
                    " WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                (attempt,) = cur.fetchall()
                assert (attempt["outcome"], attempt["executor_id"]) == (
                    "SUCCEEDED", "live-w-1",
                )
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_concurrent_claims_single_winner() -> None:
    _live_config()
    key = f"live-3cb1-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        _enqueue(action_id, "live-sched-1")
        results: list = []
        errors: list = []

        def _race(n):
            try:
                results.append(
                    _claim(action_id, "live-w-1")
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_race, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert sum(1 for r in results if r.claimed) == 1
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM external_action_attempts"
                    " WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                assert cur.fetchone()["n"] == 1
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_claim_rollback_leaves_pending_without_attempt() -> None:
    _live_config()
    key = f"live-3cb1-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        _enqueue(action_id, "live-sched-1")
        real_insert = ledger.insert_event_in_transaction
        try:
            ledger.insert_event_in_transaction = lambda conn, event: (_ for _ in ()).throw(
                DatabaseUnavailableError("event_insert_failed")
            )
            with pytest.raises(DatabaseUnavailableError):
                _claim(action_id, "live-w-1")
        finally:
            ledger.insert_event_in_transaction = real_insert
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, attempt_count FROM external_actions WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                row = cur.fetchone()
                assert (row["state"], row["attempt_count"]) == ("PENDING", 0)
                cur.execute(
                    "SELECT count(*) AS n FROM external_action_attempts WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                assert cur.fetchone()["n"] == 0
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_wrong_proof_claim_leaves_pending_without_attempt() -> None:
    _live_config()
    key = f"live-3cb1-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        _enqueue(action_id, "live-sched-1")
        with pytest.raises(AuthenticationFailedError, match="mismatch"):
            ledger.claim_action(
                action_id=action_id,
                executor_id="live-w-1",
                executor_proof="wrong-proof-0000000000000000",
            )
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, attempt_count FROM external_actions WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                row = cur.fetchone()
                assert (row["state"], row["attempt_count"]) == ("PENDING", 0)
                cur.execute(
                    "SELECT count(*) AS n FROM external_action_attempts WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                assert cur.fetchone()["n"] == 0
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_stale_replay_and_unknown_freeze() -> None:
    _live_config()
    key = f"live-3cb1-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        _enqueue(action_id, "live-sched-1")
        claim = _claim(action_id, "live-w-1")
        assert _record(
            action_id, claim.attempt["attempt_id"], "live-w-1", to_state="UNKNOWN"
        )["state"] == "UNKNOWN"
        with pytest.raises(ledger.DeferredTransitionError):
            _record(
                action_id, claim.attempt["attempt_id"], "live-w-1", to_state="FAILED"
            )
        with pytest.raises(ledger.DeferredTransitionError):
            _probe(action_id, "FAILED")
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM external_actions WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                assert cur.fetchone()["state"] == "UNKNOWN"  # frozen, no drift
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_attempt_constraints_enforced() -> None:
    _live_config()
    psycopg = pytest.importorskip("psycopg")
    from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation

    key = f"live-3cb1-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                with pytest.raises(CheckViolation):
                    cur.execute(
                        "INSERT INTO external_action_attempts (attempt_id, action_id,"
                        " attempt_number, executor_id) VALUES (%(a)s, %(b)s, 0, 'w')",
                        {"a": str(uuid4()), "b": str(action_id)},
                    )
                conn.rollback()
                with pytest.raises(CheckViolation):
                    cur.execute(
                        "INSERT INTO external_action_attempts (attempt_id, action_id,"
                        " attempt_number, executor_id, outcome, outcome_recorded_at)"
                        " VALUES (%(a)s, %(b)s, 1, 'w', 'MAYBE', now())",
                        {"a": str(uuid4()), "b": str(action_id)},
                    )
                conn.rollback()
                with pytest.raises(CheckViolation):
                    cur.execute(
                        "INSERT INTO external_action_attempts (attempt_id, action_id,"
                        " attempt_number, executor_id, outcome)"
                        " VALUES (%(a)s, %(b)s, 1, 'w', 'SUCCEEDED')",
                        {"a": str(uuid4()), "b": str(action_id)},
                    )
                conn.rollback()
                with pytest.raises(ForeignKeyViolation):
                    cur.execute(
                        "INSERT INTO external_action_attempts (attempt_id, action_id,"
                        " attempt_number, executor_id) VALUES (%(a)s, %(b)s, 1, 'w')",
                        {"a": str(uuid4()), "b": str(uuid4())},
                    )
                conn.rollback()
                first = uuid4()
                cur.execute(
                    "INSERT INTO external_action_attempts (attempt_id, action_id,"
                    " attempt_number, executor_id) VALUES (%(a)s, %(b)s, 1, 'w')",
                    {"a": str(first), "b": str(action_id)},
                )
                conn.commit()
                with pytest.raises(UniqueViolation):
                    cur.execute(
                        "INSERT INTO external_action_attempts (attempt_id, action_id,"
                        " attempt_number, executor_id) VALUES (%(a)s, %(b)s, 1, 'w')",
                        {"a": str(uuid4()), "b": str(action_id)},
                    )
                conn.rollback()
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_unauthorized_enqueue_leaves_authorized() -> None:
    _live_config()
    key = f"live-3c2b-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        with pytest.raises(AuthorizationDeniedError, match="not permitted to enqueue"):
            _enqueue(action_id, "sched-denied")
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM external_actions WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                assert cur.fetchone()["state"] == "AUTHORIZED"
                cur.execute(
                    "SELECT count(*) AS n FROM agent_events"
                    " WHERE correlation_id = %(key)s"
                    " AND event_type = 'external_action.transitioned'",
                    {"key": key},
                )
                assert cur.fetchone()["n"] == 0  # no transition audit written
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_denied_claim_rolls_back_then_permitted_claims() -> None:
    _live_config()
    key = f"live-3c2b-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        _enqueue(action_id, "live-sched-1")
        with pytest.raises(AuthorizationDeniedError, match="not permitted to claim"):
            _claim(action_id, "w-denied")
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, attempt_count FROM external_actions WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                row = cur.fetchone()
                assert (row["state"], row["attempt_count"]) == ("PENDING", 0)
                cur.execute(
                    "SELECT count(*) AS n FROM external_action_attempts WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                assert cur.fetchone()["n"] == 0
        claim = _claim(action_id, "live-w-1")  # rollback restored claimability
        assert claim.claimed is True
        assert claim.attempt["attempt_number"] == 1
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_concurrent_permitted_and_denied_claims() -> None:
    _live_config()
    key = f"live-3c2b-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        _enqueue(action_id, "live-sched-1")
        results: list = []
        denied: list = []
        errors: list = []

        def _permitted():
            try:
                results.append(_claim(action_id, "live-w-1"))
            except Exception as exc:  # collected, asserted below
                errors.append(exc)

        def _refused():
            try:
                denied.append(_claim(action_id, "w-denied"))
            except AuthorizationDeniedError as exc:
                errors.append(exc)
            except Exception as exc:  # collected, asserted below
                errors.append(exc)

        threads = [threading.Thread(target=_permitted) for _ in range(3)]
        threads += [threading.Thread(target=_refused) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Denied winners always roll back, so exactly one permitted
        # claim wins regardless of interleaving; denied racers either
        # raise or lose quietly, and never claim.
        assert sum(1 for r in results if r.claimed) == 1
        assert all(r.claimed is False for r in denied)
        assert all(isinstance(e, AuthorizationDeniedError) for e in errors)
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM external_action_attempts"
                    " WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                assert cur.fetchone()["n"] == 1
    finally:
        _live_cleanup([action_id] if action_id else [], [key])


@needs_live_pg
def test_live_non_owner_outcome_denied() -> None:
    _live_config()
    key = f"live-3c2b-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        _enqueue(action_id, "live-sched-1")
        claim = _claim(action_id, "live-w-1")
        with pytest.raises(AuthorizationDeniedError, match="only the claiming executor"):
            _record(action_id, claim.attempt["attempt_id"], "live-w-2")
        from app.properties_db import get_connection

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state FROM external_actions WHERE action_id = %(id)s",
                    {"id": action_id},
                )
                assert cur.fetchone()["state"] == "ATTEMPTING"
                cur.execute(
                    "SELECT outcome FROM external_action_attempts"
                    " WHERE attempt_id = %(id)s",
                    {"id": claim.attempt["attempt_id"]},
                )
                assert cur.fetchone()["outcome"] is None
    finally:
        _live_cleanup([action_id] if action_id else [], [key])
