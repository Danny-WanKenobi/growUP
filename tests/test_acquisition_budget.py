"""Acquisition budget + transaction-ordering coverage (mocked + PG-gated).

Coordinator acquisition must reserve one shared monthly unit before any
provider transmission, respect the durable per-run bound, refuse
exhausted budgets without calling the provider, and never retry an
ambiguous attempt. Fresh runs must commit before another connection
accounts an attempt, and the provider must transmit with no business
transaction open. Mocked tests use scripted connections (no PG, no
network); ``GROWUP_PG_INTEGRATION=1`` tests prove the real
cross-connection visibility on a disposable database.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.agents.raphael import acquire as acquire_module
from app.agents.gabriel.sources.brave import BraveTimeout
from app.research_budget_db import BudgetExhausted
from app.workflow_db import WorkflowError


class _ScriptCursor:
    def __init__(self, script: list[Any], log: list[str]) -> None:
        self._script = list(script)
        self._log = log

    def __enter__(self) -> _ScriptCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append(sql)

    def fetchone(self) -> Any:
        if not self._script:
            return None
        return self._script.pop(0)


class _ScriptConn:
    """Owned-connection double with commit/close accounting."""

    def __init__(self, script: list[Any]) -> None:
        self.statements: list[str] = []
        self._cursor = _ScriptCursor(script, self.statements)
        self.commits = 0
        self.closes = 0

    def cursor(self) -> _ScriptCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closes += 1


def _run_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "run_id": uuid4(),
        "idempotency_key": "k",
        "stage": "created",
        "status": "open",
        "correlation_id": "",
        "limits": {"brave_attempts": 9},
        "usage": {},
        "errors": [],
    }
    row.update(overrides)
    return row


def _artifact_row(payload: dict[str, Any]) -> dict[str, Any]:
    from app.workflow import digest_payload

    return {
        "record_id": uuid4(),
        "run_id": uuid4(),
        "kind": "research_artifact",
        "record_key": payload.get("assignment_id", "a1"),
        "version": 1,
        "payload": payload,
        "digest": digest_payload(payload),
    }


def _provider_item() -> SimpleNamespace:
    return SimpleNamespace(
        model_dump=lambda mode=None: {
            "url": "https://broker-a.example/x", "title": "T", "snippet": "S",
            "domain": "broker-a.example", "published_at": None,
            "retrieved_at": "2026-10-30T00:00:00+00:00", "rank": 1,
        }
    )


def _completed_payload(aid: str = "a1") -> dict[str, Any]:
    return {
        "provider": "brave", "assignment_id": aid, "query": "q",
        "results": [], "retrieved_at": "2026-10-30T00:00:00+00:00",
        "collection_provenance": "adapter_collected",
        "attempt": "completed", "error": "",
    }


def _connect_many(conns: list[_ScriptConn]):
    def _connect(config: Any = None) -> _ScriptConn:
        assert conns, "unexpected extra connection"
        return conns.pop(0)

    return _connect


# --- Mocked: monthly + per-run budget guards -------------------------------


def test_monthly_exhaustion_blocks_provider_before_transmit(monkeypatch) -> None:
    run_id = uuid4()
    read_conn = _ScriptConn([None, _run_row(run_id=run_id)])
    begin_conn = _ScriptConn([_run_row(run_id=run_id)])
    conns = [read_conn, begin_conn]
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many(conns)
    )

    def _exhausted(count: int, config: Any = None) -> Any:
        raise BudgetExhausted("empty")

    monkeypatch.setattr(acquire_module, "reserve_requests", _exhausted)

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("provider must not transmit when monthly budget is exhausted")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    with pytest.raises(WorkflowError, match="monthly Brave budget exhausted"):
        acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)
    assert begin_conn.commits == 0  # nothing charged, nothing marked


def test_per_run_bound_checked_before_monthly_reserve(monkeypatch) -> None:
    run_id = uuid4()
    spent = _run_row(run_id=run_id, usage={"brave_attempts": 1}, limits={"brave_attempts": 1})
    read_conn = _ScriptConn([None, spent])
    begin_conn = _ScriptConn([spent])
    conns = [read_conn, begin_conn]
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many(conns)
    )
    reserved: list[int] = []

    def _reserve(count: int, config: Any = None) -> Any:
        reserved.append(count)
        return {"used": 1, "ceiling": 100, "remaining": 99, "month": "2026-10"}

    monkeypatch.setattr(acquire_module, "reserve_requests", _reserve)

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("provider must not transmit past the run bound")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    with pytest.raises(WorkflowError, match="run Brave attempt bound reached"):
        acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)
    assert reserved == []  # one-request bound refuses before touching monthly
    assert begin_conn.commits == 0


def test_success_charges_shared_accounting_exactly_once(monkeypatch) -> None:
    run_id = uuid4()
    row = _run_row(run_id=run_id)
    payload_holder: dict[str, Any] = {}

    class _PersistCursor(_ScriptCursor):
        def fetchone(self) -> Any:
            from app.workflow import digest_payload

            return {
                "record_id": uuid4(), "run_id": run_id,
                "kind": "research_artifact", "record_key": "a1",
                "version": 1, "payload": payload_holder["payload"],
                "digest": digest_payload(payload_holder["payload"]),
            }

    class _PersistConn(_ScriptConn):
        def cursor(self) -> Any:
            return _PersistCursor([], self.statements)

    read_conn = _ScriptConn([None, row])
    begin_conn = _ScriptConn([row])
    persist_conn = _PersistConn([])
    real_put = acquire_module.put_record_in_transaction

    def _capture(conn: Any, run: Any, **kwargs: Any) -> Any:
        payload_holder["payload"] = kwargs["payload"]
        return real_put(conn, run, **kwargs)

    monkeypatch.setattr(acquire_module, "put_record_in_transaction", _capture)
    monkeypatch.setattr(
        "app.properties_db.get_connection",
        _connect_many([read_conn, begin_conn, persist_conn]),
    )
    reserved: list[int] = []

    def _reserve(count: int, config: Any = None) -> Any:
        reserved.append(count)
        return {"used": 1, "ceiling": 100, "remaining": 99, "month": "2026-10"}

    monkeypatch.setattr(acquire_module, "reserve_requests", _reserve)
    transmitted: list[str] = []

    def _ok(query: str, aid: str, timeout: float) -> Any:
        transmitted.append(aid)
        return [_provider_item()]

    monkeypatch.setattr(acquire_module, "execute_query", _ok)
    stored = acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)
    assert stored["attempt"] == "completed"
    assert reserved == [1]  # exactly one monthly unit for one transmission
    assert transmitted == ["a1"]
    assert begin_conn.commits == 1 and persist_conn.commits == 1


def test_ambiguous_attempt_persists_then_blocks_blind_replay(monkeypatch) -> None:
    run_id = uuid4()
    row = _run_row(run_id=run_id)
    saved: dict[str, Any] = {}

    class _PersistCursor(_ScriptCursor):
        def fetchone(self) -> Any:
            from app.workflow import digest_payload

            return {
                "record_id": uuid4(), "run_id": run_id,
                "kind": "research_artifact", "record_key": "a1",
                "version": 1, "payload": saved["payload"],
                "digest": digest_payload(saved["payload"]),
            }

    class _PersistConn(_ScriptConn):
        def cursor(self) -> Any:
            return _PersistCursor([], self.statements)

    read_conn = _ScriptConn([None, row])
    begin_conn = _ScriptConn([row])
    persist_conn = _PersistConn([])
    real_put = acquire_module.put_record_in_transaction

    def _capture(conn: Any, run: Any, **kwargs: Any) -> Any:
        saved["payload"] = kwargs["payload"]
        return real_put(conn, run, **kwargs)

    monkeypatch.setattr(acquire_module, "put_record_in_transaction", _capture)
    monkeypatch.setattr(
        "app.properties_db.get_connection",
        _connect_many([read_conn, begin_conn, persist_conn]),
    )
    reserved: list[int] = []
    monkeypatch.setattr(
        acquire_module, "reserve_requests",
        lambda count, config=None: reserved.append(count) or {
            "used": 1, "ceiling": 100, "remaining": 99, "month": "2026-10"},
    )

    def _timeout(query: str, aid: str, timeout: float) -> Any:
        raise BraveTimeout("slow")

    monkeypatch.setattr(acquire_module, "execute_query", _timeout)
    with pytest.raises(WorkflowError, match="acquisition ambiguous"):
        acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)
    assert saved["payload"]["attempt"] == "ambiguous"  # attributable artifact kept
    assert reserved == [1]  # failed transmission still counts

    # Blind replay of the same id is refused without new spend or transmit.
    replay_conn = _ScriptConn([_artifact_row(saved["payload"])])
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many([replay_conn])
    )

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("ambiguous artifact must never silently retry")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    with pytest.raises(WorkflowError, match="retry requires a new assignment id"):
        acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)
    assert reserved == [1]


def test_started_marker_without_artifact_blocks_same_id(monkeypatch) -> None:
    run_id = uuid4()
    marked = _run_row(
        run_id=run_id, errors=[{"assignment_id": "a1", "attempt": "started"}]
    )
    read_conn = _ScriptConn([None, marked])
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many([read_conn])
    )

    def _reserve_bomb(count: int, config: Any = None) -> Any:
        raise AssertionError("uncertain prior attempt must not reserve again")

    monkeypatch.setattr(acquire_module, "reserve_requests", _reserve_bomb)

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("uncertain prior attempt must not retransmit")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    with pytest.raises(WorkflowError, match="ambiguous prior attempt"):
        acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)


def test_completed_artifact_replays_without_new_charge(monkeypatch) -> None:
    run_id = uuid4()
    payload = _completed_payload()
    read_conn = _ScriptConn([_artifact_row(payload)])
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many([read_conn])
    )

    def _reserve_bomb(count: int, config: Any = None) -> Any:
        raise AssertionError("replay must not reserve")

    monkeypatch.setattr(acquire_module, "reserve_requests", _reserve_bomb)

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("replay must not transmit")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    assert acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True) == payload


def test_unapproved_acquisition_refused_before_any_connection(monkeypatch) -> None:
    def _connect_bomb(config: Any = None) -> Any:
        raise AssertionError("unapproved acquisition must not touch the database")

    monkeypatch.setattr("app.properties_db.get_connection", _connect_bomb)
    with pytest.raises(WorkflowError, match="explicit approval"):
        acquire_module.ensure_artifact_owned(uuid4(), "a1", "q", approved=False)


def test_claim_rechecks_marker_under_lock(monkeypatch) -> None:
    """A claim landing between precheck and lock is refused uncharged."""
    run_id = uuid4()
    clean = _run_row(run_id=run_id)
    # Locked read observes a concurrent claim the precheck missed.
    raced = _run_row(
        run_id=run_id, errors=[{"assignment_id": "a1", "attempt": "started"}]
    )
    read_conn = _ScriptConn([None, clean])
    begin_conn = _ScriptConn([raced])
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many([read_conn, begin_conn])
    )

    def _reserve_bomb(count: int, config: Any = None) -> Any:
        raise AssertionError("raced claim must not reserve")

    monkeypatch.setattr(acquire_module, "reserve_requests", _reserve_bomb)

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("raced claim must not transmit")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    with pytest.raises(WorkflowError, match="ambiguous prior attempt"):
        acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)
    assert begin_conn.commits == 0


def test_claim_replays_completed_artifact_under_lock(monkeypatch) -> None:
    """A completion landing between precheck and lock replays uncharged."""
    run_id = uuid4()
    payload = _completed_payload()
    read_conn = _ScriptConn([None, _run_row(run_id=run_id)])
    begin_conn = _ScriptConn([_run_row(run_id=run_id), _artifact_row(payload)])
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many([read_conn, begin_conn])
    )

    def _reserve_bomb(count: int, config: Any = None) -> Any:
        raise AssertionError("locked replay must not reserve")

    monkeypatch.setattr(acquire_module, "reserve_requests", _reserve_bomb)

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("locked replay must not transmit")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    assert acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True) == payload
    assert begin_conn.commits == 0


def test_claim_refuses_stopped_run_without_charging(monkeypatch) -> None:
    run_id = uuid4()
    read_conn = _ScriptConn([None, _run_row(run_id=run_id)])
    begin_conn = _ScriptConn([_run_row(run_id=run_id, status="stopped")])
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many([read_conn, begin_conn])
    )

    def _reserve_bomb(count: int, config: Any = None) -> Any:
        raise AssertionError("stopped run must not reserve")

    monkeypatch.setattr(acquire_module, "reserve_requests", _reserve_bomb)
    with pytest.raises(WorkflowError, match="run is stopped"):
        acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)
    assert begin_conn.commits == 0


def test_legacy_transmission_helpers_are_removed() -> None:
    """No alternative entry point may transmit without the locked claim.

    The legacy in-transaction helpers could transmit against a bare
    started-marker (no monthly reservation, no race protection), so
    they were deleted outright: the owned claim path is the only way
    to reach the provider.
    """
    assert not hasattr(acquire_module, "acquire_evidence_in_transaction")
    assert not hasattr(acquire_module, "begin_attempt_in_transaction")


def test_supported_entry_rejects_demo_assignment(monkeypatch) -> None:
    def _connect_bomb(config: Any = None) -> Any:
        raise AssertionError("demo identifiers must fail before any connection")

    monkeypatch.setattr("app.properties_db.get_connection", _connect_bomb)
    with pytest.raises(WorkflowError, match="demo identifiers"):
        acquire_module.ensure_artifact_owned(uuid4(), "demo-a1", "q", approved=True)


def test_claim_caps_attempt_limit_at_eighteen(monkeypatch) -> None:
    run_id = uuid4()
    capped = _run_row(
        run_id=run_id, usage={"brave_attempts": 18}, limits={"brave_attempts": 99}
    )
    read_conn = _ScriptConn([None, capped])
    begin_conn = _ScriptConn([capped])
    monkeypatch.setattr(
        "app.properties_db.get_connection", _connect_many([read_conn, begin_conn])
    )

    def _reserve_bomb(count: int, config: Any = None) -> Any:
        raise AssertionError("capped bound must refuse before reserving")

    monkeypatch.setattr(acquire_module, "reserve_requests", _reserve_bomb)

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("capped bound must refuse before transmitting")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    with pytest.raises(WorkflowError, match="bound reached"):
        acquire_module.ensure_artifact_owned(run_id, "a1", "q", approved=True)
    assert begin_conn.commits == 0


# --- Mocked: entry-point parity + bound propagation -------------------------


def test_wondrue_entry_points_share_one_audited_path(monkeypatch) -> None:
    from app import mvp as mvp_module
    from app.wondrue import handle_command

    seen: list[dict[str, Any]] = []

    def _fake_start(assignment_id: str, query: str, **kwargs: Any) -> Any:
        seen.append({"a": assignment_id, "q": query, **kwargs})
        return {"stage": "done"}

    monkeypatch.setattr(mvp_module, "start_evaluation_audited", _fake_start)
    override = {
        "intent": "start_real_evaluation",
        "args": {
            "assignment_id": "a1", "query": "q",
            "brave_attempt_limit": 1, "timeout_seconds": 12,
            "property_id": str(uuid4()),
        },
    }
    without_conn = handle_command(
        mode="full", max_mode="full", text="", target="real",
        intent_override=dict(override), conn=None,
    )
    with_conn = handle_command(
        mode="full", max_mode="full", text="", target="real",
        intent_override=dict(override), conn=object(),
    )
    assert without_conn["data"] == {"summary": {"stage": "done"}}
    assert with_conn["data"] == {"summary": {"stage": "done"}}
    assert len(seen) == 2 and seen[0] == seen[1]  # identical safeguards
    assert seen[0]["approved"] is True
    assert seen[0]["brave_attempt_limit"] == 1
    assert seen[0]["timeout_seconds"] == 12.0


def test_wondrue_rejects_invalid_attempt_limit(monkeypatch) -> None:
    from app import mvp as mvp_module
    from app.wondrue import WondrueError, handle_command

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("invalid bound must fail before execution")

    monkeypatch.setattr(mvp_module, "start_evaluation_audited", _boom)
    for bad in (0, 19, "many"):
        with pytest.raises(WondrueError, match="brave_attempt_limit"):
            handle_command(
                mode="full", max_mode="full", text="", target="real",
                intent_override={
                    "intent": "start_real_evaluation",
                    "args": {"assignment_id": "a1", "query": "q", "brave_attempt_limit": bad},
                },
                conn=None,
            )


def test_wondrue_preview_states_single_request_bound() -> None:
    from app.wondrue import handle_command

    preview = handle_command(
        mode="ask", max_mode="full", text="", target="real",
        intent_override={
            "intent": "start_real_evaluation",
            "args": {"assignment_id": "a1", "query": "q", "brave_attempt_limit": 1},
        },
        conn=None,
    )
    assert preview["outcome"] == "preview"
    assert preview["preview"]["cost_bounds"] == {
        "brave_attempts": "<= 1 (run limit)", "meta_calls": 0
    }


def test_runner_evaluation_threads_bound_and_timeout(monkeypatch) -> None:
    from app import discovery_runner as runner_module

    pid = str(uuid4())
    seen: dict[str, Any] = {}

    def _fake_start(assignment_id: str, query: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        seen["assignment_id"] = assignment_id
        return {"run_id": str(uuid4()), "qualification": {}}

    monkeypatch.setattr("app.mvp.start_evaluation_audited", _fake_start)
    monkeypatch.setattr(
        "app.properties_db.get_property",
        lambda target, config=None: {
            "street_address": "500 Commerce St", "city": "Dallas",
            "state": "TX", "zip_code": "75202",
        },
    )
    outcome = runner_module.evaluate_property(
        "pilot", pid, approved=True, timeout_seconds=12.0, brave_attempt_limit=1,
    )
    assert outcome["outcome"] == "evaluated"
    assert seen["brave_attempt_limit"] == 1
    assert seen["timeout_seconds"] == 12.0
    assert seen["property_id"] == pid
    assert seen["assignment_id"] == f"runner:pilot:{pid}"
    assert seen["approved"] is True


def test_runner_config_validates_evaluation_bounds() -> None:
    from app.discovery_runner import RunnerCell, RunnerConfig, RunnerError

    cell = RunnerCell(county="dallas county", category="high_rise", bbox=(1, 1, 2, 2))
    base = {"scope": "s", "policy": "pilot", "cells": [cell]}
    assert RunnerConfig(**base).evaluation_timeout == 15.0  # ordinary default
    assert RunnerConfig(**base, evaluation_timeout_seconds=12.0).evaluation_timeout == 12.0
    with pytest.raises(RunnerError, match="evaluation_attempt_limit"):
        RunnerConfig(**base, evaluation_attempt_limit=0).validate()
    with pytest.raises(RunnerError, match="evaluation_attempt_limit"):
        RunnerConfig(**base, evaluation_attempt_limit=19).validate()
    with pytest.raises(RunnerError, match="evaluation_timeout_seconds"):
        RunnerConfig(**base, evaluation_timeout_seconds=0).validate()


# --- Live PostgreSQL (GROWUP_PG_INTEGRATION=1, mocked provider) --------------

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _require_tables() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.workflow_runs') AS runs, "
                "to_regclass('public.workflow_records') AS records, "
                "to_regclass('public.research_budget') AS budget"
            )
            row = cur.fetchone()
    if row is None or row["runs"] is None or row["records"] is None or row["budget"] is None:
        pytest.skip("workflow/budget tables not present (migrations not applied)")


def _cleanup_runs(run_ids: list[str]) -> None:
    from app.properties_db import get_connection

    ids = [str(item) for item in run_ids if item]
    if not ids:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM workflow_records WHERE run_id = ANY(%(ids)s)",
                {"ids": ids},
            )
            cur.execute(
                "DELETE FROM workflow_runs WHERE run_id = ANY(%(ids)s)",
                {"ids": ids},
            )
        conn.commit()


@needs_live_pg
def test_live_uncommitted_run_invisible_to_second_connection(monkeypatch) -> None:
    """Demonstrate why the run must commit before attempt accounting.

    A run created inside one open transaction is invisible to a second
    connection (READ COMMITTED): this is the exact failure the phased
    ordering corrects. Rollback-only; no residue.
    """
    from app.properties_db import get_connection
    from app.workflow_db import create_run_in_transaction, get_run_in_transaction

    _require_tables()
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    first = get_connection()
    second = get_connection()
    try:
        run = create_run_in_transaction(
            first, idempotency_key=f"visibility-{uuid4().hex}",
            limits={"brave_attempts": 1},
        )
        assert get_run_in_transaction(second, run.run_id) is None
        assert get_run_in_transaction(first, run.run_id) is not None
    finally:
        first.rollback()
        first.close()
        second.close()


@needs_live_pg
def test_live_fresh_run_acquires_after_commit_with_mocked_provider(monkeypatch) -> None:
    """Committed run -> budgeted mocked acquisition -> persisted artifact."""
    from app.properties_db import get_connection
    from app.research_budget_db import current_month_key, read_budget
    from app.workflow_db import create_run

    _require_tables()
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setattr(
        acquire_module, "execute_query", lambda q, aid, t: [_provider_item()]
    )
    before = read_budget()
    created: list[str] = []
    try:
        run = create_run(
            idempotency_key=f"acquire-{uuid4().hex}",
            limits={"brave_attempts": 1},
        )
        created.append(str(run.run_id))
        stored = acquire_module.ensure_artifact_owned(
            run.run_id, f"live-{uuid4().hex}", "500 Commerce St, Dallas, TX",
            approved=True, timeout_seconds=12.0,
        )
        assert stored["attempt"] == "completed"
        assert stored["collection_provenance"] == "adapter_collected"
        after = read_budget()
        assert after["used"] == before["used"] + 1  # one shared unit, once
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT usage, errors FROM workflow_runs WHERE run_id = %(rid)s",
                    {"rid": str(run.run_id)},
                )
                row = cur.fetchone()
        assert row["usage"]["brave_attempts"] == 1
        assert row["errors"][-1]["attempt"] == "started"
    finally:
        _cleanup_runs(created)
        with get_connection() as conn:  # restore shared month usage
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE research_budget SET used = %(used)s "
                    "WHERE research_month = %(month)s",
                    {"used": before["used"], "month": current_month_key()},
                )
            conn.commit()


@needs_live_pg
def test_live_monthly_exhaustion_blocks_provider_on_postgres(monkeypatch) -> None:
    """Exhausted month refuses transmission without touching run usage."""
    from app.properties_db import get_connection
    from app.research_budget_db import current_month_key, read_budget, reserve_requests
    from app.workflow_db import create_run

    _require_tables()
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("exhausted budget must not transmit on PostgreSQL")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    before = read_budget()
    created: list[str] = []
    try:
        reserve_requests(1)  # guarantee used >= 1 for the ceiling clamp below
        drained = read_budget()
        monkeypatch.setenv("BRAVE_MONTHLY_CEILING", str(drained["used"]))
        run = create_run(
            idempotency_key=f"exhaust-{uuid4().hex}",
            limits={"brave_attempts": 1},
        )
        created.append(str(run.run_id))
        with pytest.raises(WorkflowError, match="monthly Brave budget exhausted"):
            acquire_module.ensure_artifact_owned(
                run.run_id, f"live-{uuid4().hex}", "500 Commerce St, Dallas, TX",
                approved=True, timeout_seconds=12.0,
            )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT usage, errors FROM workflow_runs WHERE run_id = %(rid)s",
                    {"rid": str(run.run_id)},
                )
                row = cur.fetchone()
        assert row["usage"].get("brave_attempts", 0) == 0  # refused pre-charge
        assert row["errors"] == []
    finally:
        _cleanup_runs(created)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE research_budget SET used = %(used)s "
                    "WHERE research_month = %(month)s",
                    {"used": before["used"], "month": current_month_key()},
                )
            conn.commit()


@needs_live_pg
def test_live_single_request_bound_holds_across_assignment_ids(monkeypatch) -> None:
    """Limit-1 run: first id transmits, second id is refused untransmitted."""
    from app.properties_db import get_connection
    from app.research_budget_db import current_month_key, read_budget
    from app.workflow_db import create_run

    _require_tables()
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    transmitted: list[str] = []

    def _counting(query: str, aid: str, timeout: float) -> Any:
        transmitted.append(aid)
        return [_provider_item()]

    monkeypatch.setattr(acquire_module, "execute_query", _counting)
    before = read_budget()
    created: list[str] = []
    try:
        run = create_run(
            idempotency_key=f"bound-{uuid4().hex}",
            limits={"brave_attempts": 1},
        )
        created.append(str(run.run_id))
        first = acquire_module.ensure_artifact_owned(
            run.run_id, f"live-first-{uuid4().hex}", "500 Commerce St, Dallas, TX",
            approved=True, timeout_seconds=12.0,
        )
        assert first["attempt"] == "completed"
        with pytest.raises(WorkflowError, match="run Brave attempt bound reached"):
            acquire_module.ensure_artifact_owned(
                run.run_id, f"live-second-{uuid4().hex}", "600 Main St, Dallas, TX",
                approved=True, timeout_seconds=12.0,
            )
        assert len(transmitted) == 1
        after = read_budget()
        assert after["used"] == before["used"] + 1  # refused id charged nothing
    finally:
        _cleanup_runs(created)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE research_budget SET used = %(used)s "
                    "WHERE research_month = %(month)s",
                    {"used": before["used"], "month": current_month_key()},
                )
            conn.commit()


@needs_live_pg
def test_live_two_connections_race_one_assignment(monkeypatch) -> None:
    """Two racers, run limit 9: exactly one transmits and one charge lands.

    The loser either refuses on the winner's marker or replays the
    winner's artifact; either way only one provider invocation and
    one monthly/per-run charge exist afterward.
    """
    import threading

    from app.properties_db import get_connection
    from app.research_budget_db import current_month_key, read_budget
    from app.workflow_db import create_run

    _require_tables()
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    transmitted: list[str] = []
    gate = threading.Barrier(2)
    guard = threading.Lock()

    def _slow(query: str, aid: str, timeout: float) -> Any:
        import time as _time

        with guard:
            transmitted.append(aid)
        _time.sleep(2.0)  # widen the race window
        return [_provider_item()]

    monkeypatch.setattr(acquire_module, "execute_query", _slow)
    before = read_budget()
    created: list[str] = []
    outcomes: list[str] = []
    try:
        run = create_run(
            idempotency_key=f"race-{uuid4().hex}",
            limits={"brave_attempts": 9},
        )
        created.append(str(run.run_id))
        aid = f"live-race-{uuid4().hex}"

        def _racer() -> None:
            gate.wait(timeout=30)
            try:
                acquire_module.ensure_artifact_owned(
                    run.run_id, aid, "500 Commerce St, Dallas, TX",
                    approved=True, timeout_seconds=12.0,
                )
            except WorkflowError as exc:
                outcomes.append(str(exc))
            else:
                outcomes.append("completed")

        workers = [threading.Thread(target=_racer) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        assert not any(worker.is_alive() for worker in workers)
        assert len(transmitted) == 1
        after = read_budget()
        assert after["used"] == before["used"] + 1
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT usage FROM workflow_runs WHERE run_id = %(rid)s",
                    {"rid": str(run.run_id)},
                )
                row = cur.fetchone()
                cur.execute(
                    "SELECT COUNT(*) AS total FROM workflow_records "
                    "WHERE run_id = %(rid)s AND kind = 'research_artifact' "
                    "AND record_key = %(key)s",
                    {"rid": str(run.run_id), "key": aid},
                )
                artifacts = cur.fetchone()
        assert row["usage"]["brave_attempts"] == 1
        assert artifacts["total"] == 1
        assert len(outcomes) == 2 and outcomes.count("completed") >= 1
        for other in outcomes:
            if other != "completed":  # loser refused on the winner's marker
                assert "ambiguous prior attempt" in other or "new assignment id" in other
    finally:
        _cleanup_runs(created)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE research_budget SET used = %(used)s "
                    "WHERE research_month = %(month)s",
                    {"used": before["used"], "month": current_month_key()},
                )
            conn.commit()


@needs_live_pg
def test_live_claim_rechecks_under_held_lock(monkeypatch) -> None:
    """Deterministic proof the recheck happens under the row lock.

    The worker's unlocked precheck observes a clean run, then blocks
    on the held row lock; after the holder commits a started-marker,
    the worker must refuse without transmitting or reserving — a
    pre-lock-only check would have charged and transmitted.
    """
    import threading

    from app.properties_db import get_connection
    from app.research_budget_db import current_month_key, read_budget
    from app.workflow_db import (
        add_usage_in_transaction,
        append_error_in_transaction,
        create_run,
        get_run_in_transaction,
    )

    _require_tables()
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")

    def _boom(query: str, aid: str, timeout: float) -> Any:
        raise AssertionError("locked recheck must refuse before transmitting")

    monkeypatch.setattr(acquire_module, "execute_query", _boom)
    before = read_budget()
    created: list[str] = []
    holder = get_connection()
    outcome: list[str] = []
    try:
        run = create_run(
            idempotency_key=f"lock-{uuid4().hex}",
            limits={"brave_attempts": 9},
        )
        created.append(str(run.run_id))
        aid = f"live-lock-{uuid4().hex}"
        # Hold the run lock with no marker committed yet.
        assert get_run_in_transaction(holder, run.run_id, for_update=True) is not None

        def _worker() -> None:
            try:
                acquire_module.ensure_artifact_owned(
                    run.run_id, aid, "500 Commerce St, Dallas, TX",
                    approved=True, timeout_seconds=12.0,
                )
            except WorkflowError as exc:
                outcome.append(str(exc))
            else:
                outcome.append("completed")

        worker = threading.Thread(target=_worker)
        worker.start()
        import time as _time

        _time.sleep(1.0)  # let the worker precheck clean, then block on the lock
        # Commit a rival claim while the worker waits (or before it locks;
        # either way its locked recheck must observe this marker).
        add_usage_in_transaction(holder, run.run_id, "brave_attempts", 1)
        append_error_in_transaction(
            holder, run.run_id, {"assignment_id": aid, "attempt": "started"}
        )
        holder.commit()
        worker.join(timeout=60)
        assert not worker.is_alive()
        assert len(outcome) == 1 and "ambiguous prior attempt" in outcome[0]
        after = read_budget()
        assert after["used"] == before["used"]  # refused before any reserve
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT usage FROM workflow_runs WHERE run_id = %(rid)s",
                    {"rid": str(run.run_id)},
                )
                row = cur.fetchone()
        assert row["usage"]["brave_attempts"] == 1  # holder's charge only
    finally:
        try:
            holder.rollback()
        except Exception:
            pass
        try:
            holder.close()
        except Exception:
            pass
        _cleanup_runs(created)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE research_budget SET used = %(used)s "
                    "WHERE research_month = %(month)s",
                    {"used": before["used"], "month": current_month_key()},
                )
            conn.commit()


@needs_live_pg
def test_live_started_marker_blocks_uncertain_replay_on_postgres(monkeypatch) -> None:
    """Committed marker without artifact refuses the same id (no transmit)."""
    from app.properties_db import get_connection
    from app.research_budget_db import current_month_key, read_budget
    from app.workflow_db import create_run

    _require_tables()
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    before = read_budget()
    created: list[str] = []
    try:
        run = create_run(
            idempotency_key=f"crash-{uuid4().hex}",
            limits={"brave_attempts": 9},
        )
        created.append(str(run.run_id))
        aid = f"live-{uuid4().hex}"
        acquire_module.begin_attempt(run.run_id, aid, approved=True)

        def _boom(query: str, aid2: str, timeout: float) -> Any:
            raise AssertionError("uncertain attempt must not retransmit")

        monkeypatch.setattr(acquire_module, "execute_query", _boom)
        with pytest.raises(WorkflowError, match="ambiguous prior attempt"):
            acquire_module.ensure_artifact_owned(
                run.run_id, aid, "500 Commerce St, Dallas, TX",
                approved=True, timeout_seconds=12.0,
            )
        after = read_budget()
        assert after["used"] == before["used"] + 1  # first charge kept, none added
    finally:
        _cleanup_runs(created)
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE research_budget SET used = %(used)s "
                    "WHERE research_month = %(month)s",
                    {"used": before["used"], "month": current_month_key()},
                )
            conn.commit()
