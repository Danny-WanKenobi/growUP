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
from datetime import datetime, timezone
from uuid import uuid4

import pytest

import app.external_actions_db as ledger
from app.db import DatabaseUnavailableError
from app.observability import AgentId

_T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _auth(**overrides):
    base = {
        "idempotency_key": f"key-{uuid4().hex}",
        "action_type": "outreach_email",
        "agent": AgentId.MICHAEL,
        "request": {"template": "owner_intro"},
        "authorized_by": "operator:alice",
        "authorization_reference": "ticket-123",
    }
    base.update(overrides)
    return base


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
    updated = ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    assert updated["state"] == "PENDING"
    assert updated["action_id"] == action["action_id"]
    assert (conn.commits, conn.rollbacks) == (1, 0)
    (sql, params), *_ = _audits(conn)
    assert params["event_type"] == "external_action.transitioned"
    assert params["retention_class"] == "permanent"
    details = json.loads(params["details"])
    assert (details["from_state"], details["to_state"]) == ("AUTHORIZED", "PENDING")
    assert details["attempt_id"] is None


def test_pending_to_failed_deferred_to_cancellation_protocol(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    audits_before = len(_audits(conn))
    with pytest.raises(ledger.DeferredTransitionError, match="cancellation"):
        ledger.transition_action(action_id=action["action_id"], to_state="FAILED")
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
        ledger.transition_action(
            action_id=action["action_id"], to_state="PENDING", attempt_id=uuid4()
        )


# --- Atomic claiming ---


def test_claim_success_creates_attempt_and_audit(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    result = ledger.claim_action(
        action_id=action["action_id"], executor_id="worker-1"
    )
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
    assert conn.commits == 2  # enqueue + claim on this handle


def test_claim_loses_quietly_when_not_pending(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    before = len(_audits(conn))
    lost = ledger.claim_action(action_id=action["action_id"], executor_id="worker-1")
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
    lost = ledger.claim_action(action_id=uuid4(), executor_id="worker-1")
    assert (lost.claimed, lost.action, lost.attempt) == (False, None, None)


def test_repeated_claim_second_loses(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    first = ledger.claim_action(action_id=action["action_id"], executor_id="w-1")
    second = ledger.claim_action(action_id=action["action_id"], executor_id="w-2")
    assert (first.claimed, second.claimed) == (True, False)
    assert second.action["state"] == "ATTEMPTING"
    assert len(tables.attempts) == 1


def test_concurrent_claims_single_winner(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    results: list = []
    errors: list = []

    def _race(n):
        try:
            results.append(
                ledger.claim_action(
                    action_id=action["action_id"], executor_id=f"worker-{n}"
                )
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
        ledger.claim_action(action_id="not-a-uuid", executor_id="w-1")
    with pytest.raises(ValueError):
        ledger.claim_action(action_id=uuid4(), executor_id="  ")
    assert made == []


def test_claim_audit_failure_rolls_back(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    conn.audit_fails = DatabaseUnavailableError("event_insert_failed")
    with pytest.raises(DatabaseUnavailableError):
        ledger.claim_action(action_id=action["action_id"], executor_id="w-1")
    assert conn.commits == 1 and conn.rollbacks == 1  # enqueue kept, claim rolled back


def test_claim_commit_failure_reports_failure(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    conn.commit_fails = True
    with pytest.raises(DatabaseUnavailableError, match="claim_failed"):
        ledger.claim_action(action_id=action["action_id"], executor_id="w-1")


# --- Outcomes ---


def _setup_attempting(monkeypatch):
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    claim = ledger.claim_action(action_id=action["action_id"], executor_id="w-1")
    return tables, action, claim


def test_outcome_succeeded_recorded_on_attempt(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    updated = ledger.transition_action(
        action_id=action["action_id"],
        to_state="SUCCEEDED",
        attempt_id=claim.attempt["attempt_id"],
    )
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


def test_outcome_failed_and_unknown_recorded(monkeypatch) -> None:
    for outcome in ("FAILED", "UNKNOWN"):
        tables, action, claim = _setup_attempting(monkeypatch)
        updated = ledger.transition_action(
            action_id=action["action_id"],
            to_state=outcome,
            attempt_id=claim.attempt["attempt_id"],
        )
        assert updated["state"] == outcome
        assert tables.attempts[claim.attempt["attempt_id"]]["outcome"] == outcome


def test_unknown_outcome_does_not_auto_transition(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    updated = ledger.transition_action(
        action_id=action["action_id"],
        to_state="UNKNOWN",
        attempt_id=claim.attempt["attempt_id"],
    )
    assert updated["state"] == "UNKNOWN"
    assert tables.by_id[action["action_id"]]["state"] == "UNKNOWN"
    with pytest.raises(ledger.DeferredTransitionError):
        ledger.transition_action(action_id=action["action_id"], to_state="SUCCEEDED")


def test_outcome_requires_open_attempt_id(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    with pytest.raises(ValueError, match="attempt_id is required"):
        ledger.transition_action(action_id=action["action_id"], to_state="SUCCEEDED")
    assert tables.by_id[action["action_id"]]["state"] == "ATTEMPTING"


def test_stale_attempt_ids_rejected(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    good = claim.attempt["attempt_id"]
    with pytest.raises(ledger.StaleAttemptError):
        ledger.transition_action(
            action_id=action["action_id"], to_state="SUCCEEDED", attempt_id=uuid4()
        )
    other_auth = ledger.record_authorization(**_auth())
    other_id = other_auth.action["action_id"]
    ledger.transition_action(action_id=other_id, to_state="PENDING")
    other_claim = ledger.claim_action(action_id=other_id, executor_id="w-9")
    with pytest.raises(ledger.StaleAttemptError):
        ledger.transition_action(
            action_id=action["action_id"],
            to_state="SUCCEEDED",
            attempt_id=other_claim.attempt["attempt_id"],
        )
    ledger.transition_action(
        action_id=action["action_id"], to_state="SUCCEEDED", attempt_id=good
    )
    with pytest.raises(ValueError):  # terminal state rules first
        ledger.transition_action(
            action_id=action["action_id"], to_state="FAILED", attempt_id=good
        )


def test_closed_attempt_rejected_when_state_lags_defense_in_depth(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    # Unreachable via public API (outcome flips both rows atomically);
    # the check exists so a hand-edited ledger still fails closed.
    tables.attempts[claim.attempt["attempt_id"]]["outcome"] = "SUCCEEDED"
    with pytest.raises(ledger.StaleAttemptError, match="already recorded"):
        ledger.transition_action(
            action_id=action["action_id"],
            to_state="SUCCEEDED",
            attempt_id=claim.attempt["attempt_id"],
        )


def test_superseded_attempt_rejected_defense_in_depth(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    # Unreachable via public API (a second claim always loses); the
    # check exists so a hand-edited ledger still fails closed.
    tables.by_id[action["action_id"]]["attempt_count"] = 2
    with pytest.raises(ledger.StaleAttemptError, match="superseded"):
        ledger.transition_action(
            action_id=action["action_id"],
            to_state="SUCCEEDED",
            attempt_id=claim.attempt["attempt_id"],
        )


def test_transition_audit_failure_rolls_back(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    conn = _LifeConn(tables)
    conn.audit_fails = DatabaseUnavailableError("event_insert_failed")
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    with pytest.raises(DatabaseUnavailableError):
        ledger.transition_action(
            action_id=action["action_id"],
            to_state="SUCCEEDED",
            attempt_id=claim.attempt["attempt_id"],
        )
    assert (conn.commits, conn.rollbacks) == (0, 1)


# --- GRW-EXT-001: no re-execution, no requeue, no silent retry ---


def test_no_automatic_retry_after_attempting(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    lost = ledger.claim_action(action_id=action["action_id"], executor_id="w-2")
    assert lost.claimed is False  # ATTEMPTING actions are never re-claimed
    assert len(tables.attempts) == 1
    with pytest.raises(ValueError):
        ledger.transition_action(action_id=action["action_id"], to_state="PENDING")


def test_no_requeue_from_unknown(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    ledger.transition_action(
        action_id=action["action_id"],
        to_state="UNKNOWN",
        attempt_id=claim.attempt["attempt_id"],
    )
    with pytest.raises(ValueError):  # policy-level refusal
        ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    with pytest.raises(ValueError):
        ledger.transition_action(action_id=action["action_id"], to_state="ATTEMPTING")


def test_no_unrestricted_retry_from_failed(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    ledger.transition_action(
        action_id=action["action_id"],
        to_state="FAILED",
        attempt_id=claim.attempt["attempt_id"],
    )
    with pytest.raises(ledger.DeferredTransitionError, match="retry protocol"):
        ledger.transition_action(action_id=action["action_id"], to_state="PENDING")


def test_succeeded_is_terminal(monkeypatch) -> None:
    tables, action, claim = _setup_attempting(monkeypatch)
    ledger.transition_action(
        action_id=action["action_id"],
        to_state="SUCCEEDED",
        attempt_id=claim.attempt["attempt_id"],
    )
    for target in ("PENDING", "ATTEMPTING", "FAILED", "UNKNOWN", "AUTHORIZED"):
        with pytest.raises(ValueError):
            ledger.transition_action(action_id=action["action_id"], to_state=target)


def test_pending_to_attempting_requires_claim(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    with pytest.raises(ValueError, match="claim_action"):
        ledger.transition_action(action_id=action["action_id"], to_state="ATTEMPTING")


def test_invalid_targets_and_unknown_actions(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    monkeypatch.setattr(
        ledger, "get_connection", lambda config=None: _LifeConn(tables)
    )
    with pytest.raises(ValueError, match="invalid target state"):
        ledger.transition_action(action_id=action["action_id"], to_state="EXECUTING")
    with pytest.raises(ValueError):
        ledger.transition_action(action_id=action["action_id"], to_state="SUCCEEDED")
    with pytest.raises(ledger.ActionNotFoundError):
        ledger.transition_action(action_id=uuid4(), to_state="PENDING")
    with pytest.raises(ValueError, match="invalid action_id"):
        ledger.transition_action(action_id="nope", to_state="PENDING")


# --- Full lifecycle ---


def test_full_lifecycle_four_audits(monkeypatch) -> None:
    tables, action = _setup_authorized(monkeypatch)
    conn = _LifeConn(tables)
    monkeypatch.setattr(ledger, "get_connection", lambda config=None: conn)
    ledger.transition_action(action_id=action["action_id"], to_state="PENDING")
    claim = ledger.claim_action(action_id=action["action_id"], executor_id="w-1")
    ledger.transition_action(
        action_id=action["action_id"],
        to_state="SUCCEEDED",
        attempt_id=claim.attempt["attempt_id"],
    )
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
        ledger.claim_action(action_id=uuid4(), executor_id="w-1")
    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        ledger.transition_action(action_id=uuid4(), to_state="PENDING")


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
        assert ledger.transition_action(
            action_id=action_id, to_state="PENDING"
        )["state"] == "PENDING"
        claim = ledger.claim_action(action_id=action_id, executor_id="live-w-1")
        assert claim.claimed is True
        assert claim.attempt["attempt_number"] == 1
        assert ledger.transition_action(
            action_id=action_id,
            to_state="SUCCEEDED",
            attempt_id=claim.attempt["attempt_id"],
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
        ledger.transition_action(action_id=action_id, to_state="PENDING")
        results: list = []
        errors: list = []

        def _race(n):
            try:
                results.append(
                    ledger.claim_action(
                        action_id=action_id, executor_id=f"live-w-{n}"
                    )
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
        ledger.transition_action(action_id=action_id, to_state="PENDING")
        real_insert = ledger.insert_event_in_transaction
        try:
            ledger.insert_event_in_transaction = lambda conn, event: (_ for _ in ()).throw(
                DatabaseUnavailableError("event_insert_failed")
            )
            with pytest.raises(DatabaseUnavailableError):
                ledger.claim_action(action_id=action_id, executor_id="live-w-1")
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
def test_live_stale_replay_and_unknown_freeze() -> None:
    _live_config()
    key = f"live-3cb1-{uuid4().hex}"
    action_id = None
    try:
        created = ledger.record_authorization(**_auth(idempotency_key=key))
        action_id = created.action["action_id"]
        ledger.transition_action(action_id=action_id, to_state="PENDING")
        claim = ledger.claim_action(action_id=action_id, executor_id="live-w-1")
        assert ledger.transition_action(
            action_id=action_id,
            to_state="UNKNOWN",
            attempt_id=claim.attempt["attempt_id"],
        )["state"] == "UNKNOWN"
        with pytest.raises(ledger.DeferredTransitionError):
            ledger.transition_action(
                action_id=action_id,
                to_state="FAILED",
                attempt_id=claim.attempt["attempt_id"],
            )
        with pytest.raises(ledger.DeferredTransitionError):
            ledger.transition_action(action_id=action_id, to_state="FAILED")
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
