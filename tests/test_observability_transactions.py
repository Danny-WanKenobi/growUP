"""Transaction-safe consequential auditing (Phase 3C-2).

Scripted fake psycopg connections: no live database, no Brave, no
SQLite/JSONL fallback paths. Two PostgreSQL-backed integration tests
are included but skip unless GROWUP_PG_INTEGRATION=1 with a configured
database -- the default suite never touches a real server.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

import app.observability.transactions as transactions
import app.observability.store as store
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability import (
    AgentEvent,
    AgentId,
    OperationClass,
    RetentionClass,
    insert_event,
    insert_event_in_transaction,
    run_consequential_transaction,
)

_T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _event(**overrides):
    base = {
        "agent": AgentId.GABRIEL,
        "run_id": "run-1",
        "event_type": "test.decision",
        "summary": "test decision",
        "retention_class": RetentionClass.PERMANENT,
        "timestamp": _T0,
    }
    base.update(overrides)
    return AgentEvent.model_validate(base)


class _ScriptedConn:
    """Fake psycopg connection: op log plus programmable failures."""

    def __init__(self):
        self.statements: list = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.cursors = 0
        self.commit_fails = False
        self.audit_fails: Exception | None = None
        self.existing_ids: set = set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        self.cursors += 1
        return _ScriptedCursor(self)

    def commit(self):
        if self.commit_fails:
            raise RuntimeError("simulated commit failure")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


class _ScriptedCursor:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self._conn.statements.append((sql, dict(params or {})))
        if "INTO agent_events" in sql:
            if self._conn.audit_fails is not None:
                raise self._conn.audit_fails
            if (params or {}).get("event_id") in self._conn.existing_ids:
                psycopg = pytest.importorskip("psycopg")
                from psycopg.errors import UniqueViolation

                raise UniqueViolation("duplicate key value violates unique constraint")

    def fetchone(self):
        return {"event_id": "x"}


def _factory(monkeypatch, conn=None, exc=None):
    """Patch the coordinator's connection factory; count and capture."""
    made: list = []
    conn = conn if conn is not None else _ScriptedConn()

    def _make(config=None):
        made.append(config)
        if exc is not None:
            raise exc
        return conn

    monkeypatch.setattr(transactions, "get_connection", _make)
    return conn, made


def _business(conn, result="business-done"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO properties (id) VALUES (%(id)s)", {"id": "biz-1"})
    return result


# --- 1. Success commits both on one connection ---


def test_success_commits_business_and_audit_atomically(monkeypatch) -> None:
    conn, made = _factory(monkeypatch)
    seen: list = []

    def _mutate(c):
        seen.append(c)
        return _business(c)

    result = run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
        event=_event(),
        mutate=_mutate,
    )
    assert result == "business-done"
    assert len(made) == 1  # exactly one connection opened
    assert seen == [conn]  # callback got that connection
    assert conn.cursors == 2  # business + audit, same connection
    kinds = [sql for sql, _ in conn.statements]
    assert any("INTO properties" in sql for sql in kinds)
    assert any("INTO agent_events" in sql for sql in kinds)
    assert kinds.index(next(s for s in kinds if "INTO properties" in s)) < kinds.index(
        next(s for s in kinds if "INTO agent_events" in s)
    )
    assert (conn.commits, conn.rollbacks, conn.closes) == (1, 0, 1)


def test_external_classification_accepted(monkeypatch) -> None:
    conn, made = _factory(monkeypatch)
    assert (
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_EXTERNAL,
            event=_event(),
            mutate=_business,
        )
        == "business-done"
    )
    assert (conn.commits, len(made)) == (1, 1)


# --- 2-3. Either side failing rolls both back ---


def test_business_failure_rolls_back_and_propagates(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)

    def _boom(c):
        raise ValueError("business validation failed")

    with pytest.raises(ValueError, match="business validation failed"):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate=_boom,
        )
    assert conn.statements == []  # callback raised before any SQL here
    assert (conn.commits, conn.rollbacks, conn.closes) == (0, 1, 1)


def test_business_write_then_callback_error_still_rolls_back(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)

    def _write_then_boom(c):
        _business(c)
        raise RuntimeError("late business failure")

    with pytest.raises(RuntimeError, match="late business failure"):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate=_write_then_boom,
        )
    assert any("INTO properties" in sql for sql, _ in conn.statements)
    assert not any("INTO agent_events" in sql for sql, _ in conn.statements)
    assert (conn.commits, conn.rollbacks, conn.closes) == (0, 1, 1)


def test_audit_failure_rolls_back_business(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    conn.audit_fails = DatabaseUnavailableError("event_insert_failed")
    with pytest.raises(DatabaseUnavailableError):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate=_business,
        )
    assert any("INTO properties" in sql for sql, _ in conn.statements)
    assert (conn.commits, conn.rollbacks, conn.closes) == (0, 1, 1)


# --- 4-5. Connection and commit failures never report success ---


def test_connection_failure_prevents_business_execution(monkeypatch) -> None:
    _, made = _factory(monkeypatch, exc=DatabaseUnavailableError("connection_failed"))
    calls: list = []

    def _spy(c):
        calls.append(c)

    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate=_spy,
        )
    assert calls == [] and len(made) == 1


def test_not_configured_prevents_business_execution(monkeypatch) -> None:
    _, _ = _factory(monkeypatch, exc=DatabaseNotConfiguredError("database_not_configured"))
    with pytest.raises(DatabaseNotConfiguredError):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate=_business,
        )


def test_commit_failure_does_not_report_success(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    conn.commit_fails = True
    with pytest.raises(DatabaseUnavailableError, match="transaction_commit_failed"):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate=_business,
        )
    assert conn.commits == 0  # failed commit is not a commit
    assert (conn.rollbacks, conn.closes) == (1, 1)


# --- 6-8. Classification and event validation precede all I/O ---


def test_preliminary_rejected_before_connection(monkeypatch) -> None:
    _, made = _factory(monkeypatch)
    with pytest.raises(ValueError, match="consequential"):
        run_consequential_transaction(
            operation_class=OperationClass.PRELIMINARY, event=_event(), mutate=_business
        )
    assert made == []


def test_string_and_invalid_classifications_rejected(monkeypatch) -> None:
    _, made = _factory(monkeypatch)
    for bogus in ("consequential_internal", "preliminary", None, 42, object()):
        with pytest.raises(TypeError):
            run_consequential_transaction(
                operation_class=bogus, event=_event(), mutate=_business
            )
    assert made == []


def test_nonpermanent_event_rejected_before_business(monkeypatch) -> None:
    _, made = _factory(monkeypatch)
    calls: list = []
    for retention in (RetentionClass.ACTIVITY, RetentionClass.DIAGNOSTIC):
        with pytest.raises(ValueError, match="permanent"):
            run_consequential_transaction(
                operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
                event=_event(retention_class=retention),
                mutate=lambda c: calls.append(c),
            )
    with pytest.raises(TypeError):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event={"not": "an event"},
            mutate=lambda c: calls.append(c),
        )
    with pytest.raises(TypeError):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate="not-callable",
        )
    assert calls == [] and made == []


# --- 9-10. Helper and coordinator connection discipline ---


def test_insert_helper_never_commits_rolls_back_or_connects(monkeypatch) -> None:
    conn = _ScriptedConn()
    monkeypatch.setattr(
        store, "get_connection", lambda config=None: (_ for _ in ()).throw(
            AssertionError("helper must not open connections")
        ),
    )
    assert insert_event_in_transaction(conn, _event()) is not None
    assert (conn.commits, conn.rollbacks, conn.closes) == (0, 0, 0)
    (sql, params), *_ = conn.statements
    assert sql.startswith("INSERT INTO agent_events")
    assert "%(event_id)s" in sql  # parameterized, no string formatting
    assert params["retention_class"] == "permanent"


def test_insert_helper_validates_before_sql() -> None:
    conn = _ScriptedConn()
    with pytest.raises(ValueError, match="permanent"):
        insert_event_in_transaction(conn, _event(retention_class=RetentionClass.ACTIVITY))
    with pytest.raises(TypeError):
        insert_event_in_transaction(conn, "not-an-event")
    assert conn.statements == [] and conn.cursors == 0


def test_coordinator_uses_single_factory_connection(monkeypatch) -> None:
    conn, made = _factory(monkeypatch)
    # The audit helper must run on the coordinator's connection, so even
    # a rigged store-level factory cannot be reached from this path.
    monkeypatch.setattr(
        store, "get_connection", lambda config=None: (_ for _ in ()).throw(
            AssertionError("second factory must not be used")
        ),
    )
    seen: list = []

    def _mutate(c):
        seen.append(c)
        return _business(c)

    run_consequential_transaction(
        operation_class=OperationClass.CONSEQUENTIAL_EXTERNAL,
        event=_event(),
        mutate=_mutate,
    )
    assert len(made) == 1 and seen == [conn]


# --- 11. No SQLite or JSONL fallback ---


def test_no_sqlite_or_jsonl_fallback(monkeypatch) -> None:
    import sqlite3

    monkeypatch.setattr(
        sqlite3, "connect", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not touch SQLite")
        ),
    )
    _, _ = _factory(monkeypatch, exc=DatabaseUnavailableError("connection_failed"))
    with pytest.raises(DatabaseUnavailableError, match="connection_failed"):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate=_business,
        )


def test_transactions_module_has_no_fallback_imports() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(
        Path("app/observability/transactions.py").read_text(encoding="utf-8")
    )
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not {m for m in imported if "outbox" in m or "jsonlog" in m}, imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert names.isdisjoint({"audit_event", "require_durable", "emit_nonfatal"}), names


# --- 12. Existing standalone behavior unchanged ---


def test_insert_and_deliver_event_unchanged(monkeypatch) -> None:
    conn = _ScriptedConn()
    monkeypatch.setattr(store, "get_connection", lambda config=None: conn)
    event = _event()
    assert insert_event(event) is event  # own connection, own commit
    assert conn.commits == 1
    assert store.deliver_event(_event()).status == "inserted"


# --- 13. Duplicate audit IDs fail closed ---


def test_duplicate_audit_id_fails_closed(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    event = _event()
    conn.existing_ids.add(event.event_id)
    with pytest.raises(DatabaseUnavailableError, match="event_insert_failed"):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=event,
            mutate=_business,
        )
    assert any("INTO properties" in sql for sql, _ in conn.statements)
    assert (conn.commits, conn.rollbacks, conn.closes) == (0, 1, 1)


# --- 14. Transaction-boundary misuse is out of contract ---


def test_early_commit_misuse_is_documented_not_prevented(monkeypatch) -> None:
    conn, _ = _factory(monkeypatch)
    conn.audit_fails = DatabaseUnavailableError("event_insert_failed")

    def _misbehaving(c):
        _business(c)
        c.commit()  # out-of-contract: coordinator cannot undo this
        return "claimed-done"

    with pytest.raises(DatabaseUnavailableError):
        run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=_event(),
            mutate=_misbehaving,
        )
    assert conn.commits == 1  # the misuse happened; coordinator still tried rollback
    assert conn.rollbacks == 1
    assert "must not commit" in (run_consequential_transaction.__doc__ or "")
    assert "cannot reliably prevent" in (transactions.__doc__ or "")


# --- Gated PostgreSQL-backed integration ---


def _live_config():
    from app.config import get_database_config

    cfg = get_database_config()
    if not cfg.is_configured:
        pytest.skip("database not configured")
    return cfg


def _live_cleanup(ids):
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_events WHERE event_id = ANY(%(ids)s)",
                {"ids": list(ids)},
            )
        conn.commit()


@needs_live_pg
def test_live_failed_audit_leaves_no_business_row() -> None:
    from app.properties_db import get_connection

    _live_config()
    setup = _event()  # committed setup row the audit will collide with
    business_row = _event()
    columns = ", ".join(store._INSERT_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in store._INSERT_COLUMNS)

    def _mutate(conn):
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO agent_events ({columns}) VALUES ({placeholders})",
                store._insert_params(business_row),
            )
        return "business-done"

    try:
        insert_event(setup)
        with pytest.raises(DatabaseUnavailableError):
            run_consequential_transaction(
                operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
                event=setup,  # duplicate audit ID: INSERT must fail
                mutate=_mutate,
            )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_id FROM agent_events WHERE event_id = ANY(%(ids)s)",
                    {"ids": [setup.event_id, business_row.event_id]},
                )
                present = {row["event_id"] for row in cur.fetchall()}
        assert setup.event_id in present  # setup row intact, never overwritten
        assert business_row.event_id not in present  # rolled back with the audit
    finally:
        _live_cleanup([setup.event_id, business_row.event_id])


@needs_live_pg
def test_live_success_commits_both_rows() -> None:
    from app.properties_db import get_connection

    _live_config()
    audit = _event()
    business_row = _event()
    columns = ", ".join(store._INSERT_COLUMNS)
    placeholders = ", ".join(f"%({name})s" for name in store._INSERT_COLUMNS)

    def _mutate(conn):
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO agent_events ({columns}) VALUES ({placeholders})",
                store._insert_params(business_row),
            )
        return "business-done"

    try:
        assert (
            run_consequential_transaction(
                operation_class=OperationClass.CONSEQUENTIAL_EXTERNAL,
                event=audit,
                mutate=_mutate,
            )
            == "business-done"
        )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_id FROM agent_events WHERE event_id = ANY(%(ids)s)",
                    {"ids": [audit.event_id, business_row.event_id]},
                )
                present = {row["event_id"] for row in cur.fetchall()}
        assert present == {audit.event_id, business_row.event_id}
    finally:
        _live_cleanup([audit.event_id, business_row.event_id])
