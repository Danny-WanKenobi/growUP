"""Bounded discovery runner: claims, checkpoints, policies, stop/resume.

No external providers, no live database; scripted doubles only. A
PostgreSQL-gated test at the end replays the same statements against
the actual migration-created schema (rollback-only, disposable).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest

from app.db import DatabaseUnavailableError


from app.discovery_runner import (
    TIMED_TWO_HOUR_PRESET,
    RunnerCell,
    RunnerConfig,
    RunnerError,
    adopt_stalled_in_transaction,
    checkpoint_in_transaction,
    claim_work_in_transaction,
    latest_run_in_transaction,
    request_stop_in_transaction,
    run_is_stale_in_transaction,
    run_status_in_transaction,
    run_worker,
)

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeCursor:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db
        self._result: list[Any] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def execute(self, sql: str, params: Any = None) -> None:
        import json as _json

        p = dict(params or {})
        db = self._db
        if sql.startswith("INSERT INTO discovery_runs"):
            active = any(
                row["scope"] == p["scope"] and row["status"] == "running"
                for row in db["runs"]
            )
            if active:
                self._result = []
            else:
                row = {
                    "run_id": p["run"],
                    "scope": p["scope"],
                    "policy": p["policy"],
                    "status": "running",
                    "config": _json.loads(p["config"]),
                    "checkpoint": _json.loads(p["checkpoint"]),
                    "usage": {},
                    "created_by": p["created_by"],
                    "owner": p["owner"],
                    "heartbeat_at": _now(),
                    "stop_requested": False,
                    "error": None,
                    "started_at": f"t{len(db['runs'])}",
                    "finished_at": None,
                }
                db["runs"].append(row)
                self._result = [dict(row)]
        elif "FROM discovery_runs" in sql and "ORDER BY started_at DESC" in sql:
            rows = [row for row in db["runs"] if row["scope"] == p["scope"]]
            rows.sort(key=lambda row: str(row["started_at"]), reverse=True)
            self._result = [dict(rows[0])] if rows else []
        elif sql.startswith("SELECT status, stop_requested, owner FROM discovery_runs"):
            row = next(
                (item for item in db["runs"] if str(item["run_id"]) == str(p["run"])),
                None,
            )
            self._result = [dict(row)] if row else []
        elif sql.startswith("UPDATE discovery_runs SET checkpoint"):
            updated = None
            for row in db["runs"]:
                if str(row["run_id"]) != str(p["run"]) or row["status"] != "running":
                    continue
                if "AND owner =" in sql and row["owner"] != p["owner"]:
                    continue
                row["checkpoint"] = _json.loads(p["checkpoint"])
                row["heartbeat_at"] = _now()
                updated = row
            self._result = [{"run_id": updated["run_id"]}] if updated else []
        elif "SET stop_requested = TRUE" in sql:
            updated = None
            for row in db["runs"]:
                if row["scope"] == p["scope"] and row["status"] == "running":
                    row["stop_requested"] = True
                    updated = row
            self._result = [{"run_id": updated["run_id"]}] if updated else []
        elif sql.startswith("UPDATE discovery_runs SET status"):
            for row in db["runs"]:
                if str(row["run_id"]) == str(p["run"]):
                    row["status"] = p["status"]
                    row["finished_at"] = "end"
                    row["error"] = p["error"]
                    row["usage"] = _json.loads(p["usage"])
            self._result = []
        elif "AS stale FROM discovery_runs" in sql:
            row = next(
                (item for item in db["runs"] if str(item["run_id"]) == str(p["run"])),
                None,
            )
            if row is None or row["status"] != "running":
                self._result = []
            else:
                age = (_now() - row["heartbeat_at"]).total_seconds()
                self._result = [{"stale": age > float(p["lease"])}]
        elif sql.startswith("UPDATE discovery_runs SET owner"):
            updated = None
            for row in db["runs"]:
                if str(row["run_id"]) != str(p["run"]) or row["status"] != "running":
                    continue
                age = (_now() - row["heartbeat_at"]).total_seconds()
                if age <= float(p["lease"]):
                    continue
                if "AND stop_requested = FALSE" in sql and row["stop_requested"]:
                    continue
                row["owner"] = p["owner"]
                row["heartbeat_at"] = _now()
                row["stop_requested"] = False
                row["error"] = None
                updated = row
            self._result = [dict(updated)] if updated else []
        else:  # pragma: no cover - fake must cover every runner statement
            raise AssertionError(f"unhandled SQL: {sql[:80]}")


class _FakeConn:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def _db() -> dict[str, Any]:
    return {"runs": []}


def _cells() -> list[RunnerCell]:
    return [
        RunnerCell(county="dallas county", category="high_rise", bbox=(32.0, -97.0, 33.0, -96.0)),
        RunnerCell(county="tarrant county", category="high_rise", bbox=(32.0, -98.0, 33.0, -97.0)),
    ]


def _config(**overrides: Any) -> RunnerConfig:
    params: dict[str, Any] = {
        "scope": "test", "policy": "pilot", "cells": _cells(),
        "max_passes": 4, "max_duration_seconds": 600.0,
        "max_created_total": 50,
    }
    params.update(overrides)
    return RunnerConfig(**params)


class _Pass:
    def __init__(self, created: int, created_ids: tuple = ()) -> None:
        self.counts = type("Counts", (), {"created": created})()
        self.created_ids = list(created_ids)


def _no_sabbath() -> datetime:
    return datetime(2026, 10, 28, 12, 0, 0)  # Wednesday


def test_one_active_worker_per_scope() -> None:
    db = _db()
    conn = _FakeConn(db)
    first = claim_work_in_transaction(
        conn, scope="s", policy="pilot", checkpoint={"cursor": 0},
        config={"max_passes": 1}, created_by="t", owner="w1",
    )
    assert first is not None and first["status"] == "running"
    assert first["owner"] == "w1"
    assert first["config"] == {"max_passes": 1}
    assert first["stop_requested"] is False
    assert (
        claim_work_in_transaction(
            conn, scope="s", policy="pilot", checkpoint={}, owner="w2"
        )
        is None
    )
    other = claim_work_in_transaction(
        conn, scope="other", policy="pilot", checkpoint={}, owner="w2"
    )
    assert other is not None
    assert run_status_in_transaction(conn, first["run_id"]) == "running"
    assert latest_run_in_transaction(conn, "s")["run_id"] == first["run_id"]
    with pytest.raises(RunnerError):
        claim_work_in_transaction(
            conn, scope="s", policy="nope", checkpoint={}, owner="w3"
        )
    with pytest.raises(RunnerError, match="owner is required"):
        claim_work_in_transaction(conn, scope="s", policy="pilot", checkpoint={})


def test_pilot_serves_unmet_cells_first() -> None:
    seen: list[str] = []

    def _discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        seen.append(cell.key)
        return _Pass(0)

    db = _db()
    counts = {("dallas county", "high_rise"): 4, ("tarrant county", "high_rise"): 0}
    report = run_worker(
        lambda: _FakeConn(db), _config(max_passes=1),
        discovery_fn=_discovery, counts_fn=lambda: dict(counts),
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert seen == ["tarrant county||high_rise"]


def test_pilot_stops_when_quota_met() -> None:
    from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER
    from app.pilot import PILOT_CATEGORIES

    counts = {
        (county, category): 4
        for county in COUNTY_PRIORITY_ORDER
        for category in PILOT_CATEGORIES
    }
    calls: list[str] = []
    db = _db()
    report = run_worker(
        lambda: _FakeConn(db), _config(),
        discovery_fn=lambda cell, run_id="", limit=0: (calls.append(cell.key), _Pass(0))[1],
        counts_fn=lambda: dict(counts),
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert report["reason"] == "pilot quota satisfied"
    assert calls == []


def test_timed_continues_past_quota() -> None:
    from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER
    from app.pilot import PILOT_CATEGORIES

    counts = {
        (county, category): 9
        for county in COUNTY_PRIORITY_ORDER
        for category in PILOT_CATEGORIES
    }
    calls: list[str] = []
    db = _db()
    report = run_worker(
        lambda: _FakeConn(db), _config(policy="timed", max_passes=2),
        discovery_fn=lambda cell, run_id="", limit=0: (calls.append(cell.key), _Pass(1))[1],
        counts_fn=lambda: dict(counts),
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert len(calls) == 2
    assert report["created"] == 2


def test_limits_stop_burst() -> None:
    db = _db()
    report = run_worker(
        lambda: _FakeConn(db), _config(max_passes=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(3),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert (report["outcome"], report["passes"], report["created"]) == (
        "completed", 1, 3,
    )
    tick = {"now": 0.0}
    db2 = _db()
    report = run_worker(
        lambda: _FakeConn(db2), _config(max_passes=10, max_duration_seconds=5.0),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        clock_fn=lambda: tick.__setitem__("now", tick["now"] + 10.0) or tick["now"],
        now_fn=_no_sabbath,
    )
    assert report["outcome"] == "paused"
    assert report["reason"] == "duration limit reached"


def test_stop_and_resume_without_duplicate_work() -> None:
    db = _db()
    seen: list[str] = []

    def _stop_after_first(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        seen.append(cell.key)
        conn = _FakeConn(db)
        assert request_stop_in_transaction(conn, "test") is True
        return _Pass(0)

    first = run_worker(
        lambda: _FakeConn(db), _config(max_passes=4),
        discovery_fn=_stop_after_first, counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert first["outcome"] == "paused"
    assert first["passes"] == 1
    second = run_worker(
        lambda: _FakeConn(db), _config(max_passes=4),
        discovery_fn=lambda cell, run_id="", limit=0: (seen.append(cell.key), _Pass(0))[1],
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert second["outcome"] == "completed"
    assert second["run_id"] != first["run_id"]
    # Cursor advanced: the resumed burst starts on the other cell.
    assert seen[0] != seen[1]
    assert len(db["runs"]) == 2
    assert request_stop_in_transaction(_FakeConn(db), "idle") is False


def test_consecutive_failures_abort_with_backoff() -> None:
    sleeps: list[float] = []

    def _boom(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        raise RuntimeError("provider down")

    db = _db()
    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=9, pacing_seconds=5.0, max_consecutive_failures=3),
        discovery_fn=_boom, counts_fn=lambda: {},
        sleep_fn=sleeps.append, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "aborted"
    assert "3 consecutive provider failures" in (report["reason"] or "")
    assert sleeps == [5.0, 10.0]


def test_auth_failure_aborts_immediately() -> None:
    class AuthError(Exception):
        pass

    def _denied(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        raise AuthError("bad key")

    db = _db()
    report = run_worker(
        lambda: _FakeConn(db), _config(),
        discovery_fn=_denied, counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "aborted"
    assert "auth" in (report["reason"] or "").lower()


def test_sabbath_pauses_without_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SABBATH_BLACKOUT", raising=False)
    calls: list[str] = []
    saturday = datetime(2026, 10, 31, 12, 0, 0)
    db = _db()
    report = run_worker(
        lambda: _FakeConn(db), _config(),
        discovery_fn=lambda cell, run_id="", limit=0: (calls.append(cell.key), _Pass(0))[1],
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=lambda: saturday,
    )
    assert report["outcome"] == "paused"
    assert report["reason"] == "Sabbath blackout window"
    assert calls == []


def test_busy_scope_reports_without_claim() -> None:
    db = _db()
    assert (
        claim_work_in_transaction(
            _FakeConn(db), scope="test", policy="pilot", checkpoint={},
            owner="holder",
        )
        is not None
    )
    report = run_worker(
        lambda: _FakeConn(db), _config(),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0), counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "busy"
    assert len(db["runs"]) == 1


def test_two_hour_preset_is_documented_not_launched() -> None:
    assert TIMED_TWO_HOUR_PRESET["launched"] is False
    assert TIMED_TWO_HOUR_PRESET["max_duration_seconds"] == 7200
    assert TIMED_TWO_HOUR_PRESET["policy"] == "timed"


def test_runner_cell_selection_bypass_refused() -> None:
    cell = RunnerCell(county="dallas county", category="high_rise", bbox=(32.0, -97.0, 33.0, -96.0),
                      source="overpass", jurisdiction="Travis County", selection=None)
    with pytest.raises(RunnerError):
        RunnerConfig(scope="s", policy="pilot", cells=[cell]).validate()
    from app.agents.gabriel.sources.source_selection import bootstrap_selection
    ok = bootstrap_selection(source="overpass", jurisdiction="Travis County")
    cell2 = RunnerCell(county="dallas county", category="high_rise", bbox=(32.0, -97.0, 33.0, -96.0),
                       source="overpass", jurisdiction="Travis County", selection=ok)
    RunnerConfig(scope="s", policy="pilot", cells=[cell2]).validate()


def test_config_validation() -> None:
    with pytest.raises(RunnerError):
        RunnerConfig(scope="", policy="pilot", cells=_cells()).validate()
    with pytest.raises(RunnerError):
        RunnerConfig(scope="s", policy="nope", cells=_cells()).validate()
    with pytest.raises(RunnerError):
        RunnerConfig(scope="s", policy="pilot", cells=[]).validate()
    with pytest.raises(RunnerError, match="lease"):
        RunnerConfig(
            scope="s", policy="pilot", cells=_cells(), claim_lease_seconds=0
        ).validate()
    assert run_status_in_transaction(_FakeConn(_db()), uuid4()) is None
    with pytest.raises(RunnerError):
        run_status_in_transaction(_FakeConn(_db()), "nope")


def _stale(db: dict[str, Any]) -> None:
    db["runs"][0]["heartbeat_at"] = datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_stop_holds_slot_until_worker_exits() -> None:
    db = _db()
    conn = _FakeConn(db)
    first = claim_work_in_transaction(
        conn, scope="s", policy="pilot", checkpoint={}, owner="w1"
    )
    assert first is not None
    assert request_stop_in_transaction(conn, "s") is True
    # Slot stays held: no second worker can claim mid-flight.
    assert run_status_in_transaction(conn, first["run_id"]) == "running"
    assert (
        claim_work_in_transaction(conn, scope="s", policy="pilot", checkpoint={},
                                  owner="w2") is None
    )
    assert request_stop_in_transaction(conn, "idle") is False


def test_stale_check_and_adopt() -> None:
    db = _db()
    conn = _FakeConn(db)
    row = claim_work_in_transaction(
        conn, scope="s", policy="pilot", checkpoint={"cursor": 3}, owner="dead"
    )
    assert row is not None
    assert run_is_stale_in_transaction(conn, row["run_id"], 60.0) is False
    assert adopt_stalled_in_transaction(conn, row["run_id"], "w2", 60.0) is None
    _stale(db)
    assert run_is_stale_in_transaction(conn, row["run_id"], 60.0) is True
    adopted = adopt_stalled_in_transaction(conn, row["run_id"], "w2", 60.0)
    assert adopted is not None and adopted["owner"] == "w2"
    assert adopted["checkpoint"] == {"cursor": 3}
    assert run_is_stale_in_transaction(conn, row["run_id"], 60.0) is False
    assert run_is_stale_in_transaction(conn, uuid4(), 60.0) is None
    with pytest.raises(RunnerError):
        adopt_stalled_in_transaction(conn, "not-a-uuid", "w2", 60.0)


def test_stopped_stale_row_needs_explicit_resume() -> None:
    db = _db()
    conn = _FakeConn(db)
    row = claim_work_in_transaction(
        conn, scope="s", policy="pilot", checkpoint={}, owner="dead"
    )
    assert row is not None
    assert request_stop_in_transaction(conn, "s") is True
    _stale(db)
    # Ordinary start must not override the operator's stop.
    assert adopt_stalled_in_transaction(conn, row["run_id"], "w2", 60.0) is None
    adopted = adopt_stalled_in_transaction(
        conn, row["run_id"], "w2", 60.0, resume=True
    )
    assert adopted is not None and adopted["stop_requested"] is False


def test_worker_adopts_crashed_row_and_resumes_cursor() -> None:
    db = _db()
    conn = _FakeConn(db)
    row = claim_work_in_transaction(
        conn, scope="test", policy="pilot", checkpoint={}, owner="dead"
    )
    assert row is not None
    # Crashed worker left one completed pass behind.
    db["runs"][0]["checkpoint"] = {
        "created_total": 7, "passes": 1, "cursor": 1,
        "per_cell": {"('C1', 'high_rise')": 7},
    }
    _stale(db)
    seen: list[int] = []

    def _discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> Any:
        seen.append(len(seen))
        return _Pass(2)

    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1),
        discovery_fn=_discovery,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert report["run_id"] == str(row["run_id"])
    assert report["adopted_from"] == "dead"
    assert db["runs"][0]["checkpoint"]["cursor"] == 2
    assert db["runs"][0]["checkpoint"]["created_total"] == 9
    assert len(db["runs"]) == 1


def test_stale_stopped_row_blocks_start_until_resume() -> None:
    db = _db()
    conn = _FakeConn(db)
    row = claim_work_in_transaction(
        conn, scope="test", policy="pilot", checkpoint={}, owner="dead"
    )
    assert row is not None
    assert request_stop_in_transaction(conn, "test") is True
    _stale(db)
    report = run_worker(
        lambda: _FakeConn(db),
        _config(),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert report["outcome"] == "paused"
    assert report["run_id"] is None
    assert "resume explicitly" in (report["reason"] or "")
    resumed = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, resume=True),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert resumed["outcome"] == "completed"
    assert resumed["run_id"] == str(row["run_id"])


def test_policy_mismatch_aborts_instead_of_adopting() -> None:
    db = _db()
    conn = _FakeConn(db)
    row = claim_work_in_transaction(
        conn, scope="test", policy="pilot", checkpoint={}, owner="dead"
    )
    assert row is not None
    _stale(db)
    report = run_worker(
        lambda: _FakeConn(db),
        _config(policy="timed"),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert report["outcome"] == "aborted"
    assert "uses policy 'pilot'" in (report["reason"] or "")
    assert db["runs"][0]["owner"] == "dead"


def test_checkpoint_owner_fencing() -> None:
    db = _db()
    conn = _FakeConn(db)
    row = claim_work_in_transaction(
        conn, scope="s", policy="pilot", checkpoint={}, owner="w1"
    )
    assert row is not None
    assert checkpoint_in_transaction(conn, row["run_id"], {"a": 1}, owner="w1") is True
    assert (
        checkpoint_in_transaction(conn, row["run_id"], {"a": 2}, owner="w2") is False
    )
    assert db["runs"][0]["checkpoint"] == {"a": 1}
    with pytest.raises(RunnerError):
        checkpoint_in_transaction(conn, "not-a-uuid", {})


def test_worker_exits_when_ownership_lost_mid_pass() -> None:
    db = _db()

    def _hijack(cell: RunnerCell, run_id: str = "", limit: int = 0) -> Any:
        db["runs"][0]["owner"] = "recovery"  # adoption landed mid-pass
        return _Pass(3)

    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=2),
        discovery_fn=_hijack,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert report["outcome"] == "paused"
    assert "ownership lost" in (report["reason"] or "")
    # The ousted worker wrote nothing over the adopter's row.
    assert db["runs"][0]["checkpoint"]["created_total"] == 0
    assert db["runs"][0]["status"] == "running"


def test_sabbath_uses_chicago_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.discovery_runner import _sabbath_blackout

    monkeypatch.delenv("SABBATH_BLACKOUT", raising=False)
    monkeypatch.delenv("SABBATH_START", raising=False)
    monkeypatch.delenv("SABBATH_END", raising=False)
    friday = datetime(2026, 10, 30, tzinfo=timezone.utc)
    # 17:30 CDT (UTC-5) on Friday: before the window.
    assert _sabbath_blackout(friday.replace(hour=22, minute=30)) is False
    # 18:30 CDT on Friday: inside the window.
    assert _sabbath_blackout(friday.replace(hour=23, minute=30)) is True
    # Naive datetimes are interpreted as Chicago time.
    assert _sabbath_blackout(datetime(2026, 10, 30, 17, 30)) is False
    assert _sabbath_blackout(datetime(2026, 10, 30, 18, 30)) is True
    assert _sabbath_blackout(datetime(2026, 10, 31, 12, 0)) is True
    assert _sabbath_blackout(datetime(2026, 11, 2, 12, 0)) is False


def test_sabbath_window_configurable_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.discovery_runner import _sabbath_blackout

    monkeypatch.delenv("SABBATH_BLACKOUT", raising=False)
    monkeypatch.setenv("SABBATH_START", "sun 00:00")
    monkeypatch.setenv("SABBATH_END", "sun 01:00")
    assert _sabbath_blackout(datetime(2026, 11, 1, 0, 30)) is True
    assert _sabbath_blackout(datetime(2026, 11, 2, 12, 0)) is False
    monkeypatch.setenv("SABBATH_START", "bogus")
    assert _sabbath_blackout(datetime(2026, 11, 2, 12, 0)) is True
    monkeypatch.setenv("SABBATH_BLACKOUT", "0")
    assert _sabbath_blackout(datetime(2026, 10, 31, 12, 0)) is False


@needs_live_pg
def test_live_runner_sql_matches_migration_schema() -> None:
    """Replay every runner statement against the real migration DDL.

    Executes the discovery_runs portion of migration 015 inside one
    transaction and rolls everything back: validates column names,
    the partial-index conflict arbiter, stop ownership, staleness,
    adoption, and finish shapes. Disposable; commits nothing.
    """
    import pathlib

    from app.discovery_runner import (
        finish_run_in_transaction,
        latest_run_in_transaction,
    )
    from app.properties_db import get_connection

    migration = (
        pathlib.Path(__file__).resolve().parent.parent
        / "migrations"
        / "015_simulation_discovery_pilot.sql"
    )
    ddl = migration.read_text(encoding="utf-8")
    marker = "CREATE TABLE IF NOT EXISTS discovery_runs"
    assert marker in ddl
    ddl = ddl[ddl.index(marker):]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        scope = f"live-runner-{uuid4().hex[:12]}"
        claimed = claim_work_in_transaction(
            conn, scope=scope, policy="pilot",
            checkpoint={"cursor": 0}, config={"max_passes": 1},
            created_by="live-test", owner="live-w1",
        )
        assert claimed is not None and claimed["status"] == "running"
        for key in (
            "run_id", "scope", "policy", "status", "config", "checkpoint",
            "usage", "created_by", "owner", "heartbeat_at",
            "stop_requested", "error", "started_at", "finished_at",
        ):
            assert key in claimed, key
        # Partial-index arbiter: second claim is refused, not an error.
        assert (
            claim_work_in_transaction(
                conn, scope=scope, policy="pilot", checkpoint={},
                owner="live-w2",
            )
            is None
        )
        assert run_status_in_transaction(conn, claimed["run_id"]) == "running"
        assert request_stop_in_transaction(conn, scope) is True
        assert run_status_in_transaction(conn, claimed["run_id"]) == "running"
        assert (
            claim_work_in_transaction(
                conn, scope=scope, policy="pilot", checkpoint={},
                owner="live-w3",
            )
            is None
        )
        assert (
            checkpoint_in_transaction(
                conn, claimed["run_id"], {"cursor": 1}, owner="live-w1"
            )
            is True
        )
        assert (
            checkpoint_in_transaction(
                conn, claimed["run_id"], {"cursor": 2}, owner="live-w2"
            )
            is False
        )
        assert (
            run_is_stale_in_transaction(conn, claimed["run_id"], 3600.0) is False
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET heartbeat_at = now() - "
                "make_interval(hours => 2) WHERE run_id = %(run)s",
                {"run": claimed["run_id"]},
            )
        assert run_is_stale_in_transaction(conn, claimed["run_id"], 60.0) is True
        # Stopped row: ordinary adopt refuses, resume adopts.
        assert (
            adopt_stalled_in_transaction(conn, claimed["run_id"], "live-w4", 60.0)
            is None
        )
        adopted = adopt_stalled_in_transaction(
            conn, claimed["run_id"], "live-w4", 60.0, resume=True
        )
        assert adopted is not None and adopted["owner"] == "live-w4"
        assert adopted["stop_requested"] is False
        finish_run_in_transaction(
            conn, claimed["run_id"], "completed", None, usage={"passes": 1}
        )
        latest = latest_run_in_transaction(conn, scope)
        assert latest is not None and latest["status"] == "completed"
        assert latest["usage"] == {"passes": 1}
    finally:
        conn.rollback()
        conn.close()

def _prop(pid: str, *, street: str = "500 Commerce St") -> dict[str, Any]:
    return {
        "id": pid, "street_address": street, "city": "Dallas",
        "state": "TX", "zip_code": "75202",
    }


def test_pass_drives_created_properties_through_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass reaches truthful qualified/needs-evidence outcomes."""
    db = _db()
    pid1, pid2 = str(uuid4()), str(uuid4())
    props = {pid1: _prop(pid1), pid2: _prop(pid2, street="600 Main St")}
    calls: list[tuple] = []

    def _fake_get_property(pid: Any, config: Any = None) -> Any:
        return props.get(str(pid))

    def _fake_start(assignment_id: str, query: str, **kwargs: Any) -> Any:
        calls.append((assignment_id, query, kwargs.get("property_id")))
        if "500 Commerce" in query:
            return {
                "run_id": "r1", "decision": "needs_evidence",
                "dossier_keys": ["k1"], "qualification": {"k1": "qualified"},
            }
        return {
            "run_id": "r2", "decision": "needs_evidence",
            "dossier_keys": ["k2"], "qualification": {"k2": "incomplete"},
        }

    monkeypatch.setattr("app.properties_db.get_property", _fake_get_property)
    monkeypatch.setattr("app.mvp.start_evaluation_audited", _fake_start)
    monkeypatch.setattr("app.mvp.workflow_enabled", lambda: True)
    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, acquire_approved=True),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(2, (pid1, pid2)),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert report["created"] == 2  # property records, not qualification
    assert report["evaluated"] == 2
    assert report["qualified_dossiers"] == 1
    assert calls[0][0] == f"runner:test:{pid1}"  # deterministic assignment
    assert calls[0][2] == pid1  # bound to the discovered property
    assert "500 Commerce St, Dallas, TX 75202" in calls[0][1]
    evals = db["runs"][0]["checkpoint"]["evaluations"]
    assert evals[pid1]["decision"] == "needs_evidence"
    assert evals[pid1]["qualified"] == 1
    assert evals[pid2]["qualified"] == 0


def test_resume_skips_terminal_evaluations_reruns_errors() -> None:
    db = _db()
    pid1, pid2, pid3 = str(uuid4()), str(uuid4()), str(uuid4())
    drives: list[str] = []

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        drives.append(pid)
        if pid == pid1:
            return {"outcome": "evaluated", "decision": "needs_evidence",
                    "qualified": 1, "dossiers": 1, "run_id": "r"}
        if pid == pid2:
            return {"outcome": "blocked", "reason": "ambiguous prior attempt"}
        return {"outcome": "error", "reason": "boom"}

    kwargs: dict[str, Any] = {
        "discovery_fn": lambda cell, run_id="", limit=0: _Pass(3, (pid1, pid2, pid3)),
        "counts_fn": lambda: {},
        "sleep_fn": lambda seconds: None,
        "now_fn": _no_sabbath,
        "evaluate_fn": _eval,
    }
    first = run_worker(lambda: _FakeConn(db), _config(max_passes=1), **kwargs)
    assert first["evaluated"] == 1
    second = run_worker(lambda: _FakeConn(db), _config(max_passes=1), **kwargs)
    assert second["evaluated"] == 0  # terminal outcomes never re-driven
    assert drives == [pid1, pid2, pid3, pid3]  # only the error retried


def test_evaluations_gated_without_approval_or_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _db()
    pid = str(uuid4())
    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(1, (pid,)),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert report["evaluated"] == 0
    assert report["evaluations_precondition"] == "acquisition not approved"
    assert db["runs"][0]["checkpoint"]["evaluations"] == {}
    monkeypatch.setattr("app.mvp.workflow_enabled", lambda: False)
    gated = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, acquire_approved=True),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(1, (pid,)),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert gated["evaluated"] == 0
    assert gated["evaluations_precondition"] == "coordinator disabled"


def test_max_evaluations_per_pass_bounds_driving() -> None:
    db = _db()
    ids = tuple(str(uuid4()) for _ in range(3))
    drives: list[str] = []

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        drives.append(pid)
        return {"outcome": "evaluated", "qualified": 0}

    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, max_evaluations_per_pass=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(3, ids),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    assert drives == [ids[0]]
    assert report["evaluations_pending"] == 2


def test_evaluation_database_outage_aborts_burst() -> None:
    db = _db()
    pid = str(uuid4())

    def _down(scope: str, pid: str) -> dict[str, Any]:
        raise DatabaseUnavailableError("evaluation database unavailable")

    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(1, (pid,)),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
        evaluate_fn=_down,
    )
    assert report["outcome"] == "aborted"
    assert report["reason"] == "evaluation database unavailable"


def test_evaluate_property_skips_blocks_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.discovery_runner import build_evaluation_query, evaluate_property
    from app.workflow_db import WorkflowError

    assert build_evaluation_query(_prop("x")) == (
        "500 Commerce St, Dallas, TX 75202 commercial property"
    )
    assert build_evaluation_query({"city": "Dallas"}) is None
    with pytest.raises(RunnerError, match="acquisition approval"):
        evaluate_property("s", str(uuid4()), approved=False, timeout_seconds=1)
    assert (
        evaluate_property("s", "not-a-uuid", approved=True,
                          timeout_seconds=1)["outcome"] == "skipped"
    )
    pid = str(uuid4())
    monkeypatch.setattr(
        "app.properties_db.get_property", lambda pid, config=None: None
    )
    assert evaluate_property(
        "s", pid, approved=True, timeout_seconds=1
    ) == {"outcome": "skipped", "reason": "property row missing"}
    monkeypatch.setattr(
        "app.properties_db.get_property",
        lambda pid, config=None: {"id": pid, "city": "Dallas"},
    )
    assert evaluate_property(
        "s", pid, approved=True, timeout_seconds=1
    ) == {"outcome": "skipped", "reason": "no usable address"}
    monkeypatch.setattr(
        "app.properties_db.get_property",
        lambda pid, config=None: _prop(str(pid)),
    )
    seen: dict[str, Any] = {}

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise WorkflowError(
            "ambiguous prior attempt; retry requires a new assignment id"
        )

    monkeypatch.setattr("app.mvp.start_evaluation_audited", _refuse)
    blocked = evaluate_property("s", pid, approved=True, timeout_seconds=3)
    assert blocked["outcome"] == "blocked"
    assert seen["property_id"] == pid and seen["approved"] is True
    assert seen["timeout_seconds"] == 3

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise WorkflowError("acquisition failed: timeout")

    monkeypatch.setattr("app.mvp.start_evaluation_audited", _boom)
    assert (
        evaluate_property("s", pid, approved=True, timeout_seconds=1)["outcome"]
        == "error"
    )

def test_enqueue_backlog_dedups_and_records_discovery_runs() -> None:
    from app.discovery_runner import _enqueue_backlog, _fresh_checkpoint

    checkpoint = _fresh_checkpoint(_cells())
    assert _enqueue_backlog(checkpoint, ["a", "b"], "d1") == 2
    assert _enqueue_backlog(checkpoint, ["b", "c"], "d1") == 3
    assert checkpoint["discovery_runs"] == ["d1"]
    checkpoint["evaluations"]["a"] = {"outcome": "evaluated"}
    assert _enqueue_backlog(checkpoint, ["a", "d"], "d2") == 4
    assert checkpoint["backlog"] == ["a", "b", "c", "d"]
    assert checkpoint["discovery_runs"] == ["d1", "d2"]


def test_backlog_completes_capped_work_across_resume() -> None:
    """Ten candidates, five-eval cap: all ten terminal across bursts."""
    db = _db()
    ids = tuple(str(uuid4()) for _ in range(10))
    drives: list[str] = []

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        drives.append(pid)
        return {"outcome": "evaluated", "qualified": 0}

    kwargs: dict[str, Any] = {
        "counts_fn": lambda: {},
        "sleep_fn": lambda seconds: None,
        "now_fn": _no_sabbath,
        "evaluate_fn": _eval,
    }
    first = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, max_evaluations_per_pass=5),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(10, ids),
        **kwargs,
    )
    assert first["outcome"] == "completed"
    assert first["evaluated"] == 5
    assert first["evaluations_pending"] == 5
    assert len(db["runs"][0]["checkpoint"]["backlog"]) == 5
    # Resume: rediscovery finds nothing new, yet the backlog drives on.
    second = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, max_evaluations_per_pass=5),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0, ()),
        **kwargs,
    )
    assert second["evaluated"] == 5
    assert second["evaluations_pending"] == 0
    assert sorted(drives) == sorted(ids)  # each driven exactly once


def test_backlog_reconsiders_errors_never_repeats_blocked() -> None:
    db = _db()
    flaky, refused = str(uuid4()), str(uuid4())
    drives: list[str] = []
    attempts: dict[str, int] = {}

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        drives.append(pid)
        attempts[pid] = attempts.get(pid, 0) + 1
        if pid == refused:
            return {"outcome": "blocked", "reason": "ambiguous prior attempt"}
        if attempts[pid] == 1:
            return {"outcome": "error", "reason": "boom"}
        return {"outcome": "evaluated", "qualified": 1}

    kwargs: dict[str, Any] = {
        "discovery_fn": lambda cell, run_id="", limit=0: _Pass(2, (flaky, refused)),
        "counts_fn": lambda: {},
        "sleep_fn": lambda seconds: None,
        "now_fn": _no_sabbath,
        "evaluate_fn": _eval,
    }
    first = run_worker(lambda: _FakeConn(db), _config(max_passes=1), **kwargs)
    assert first["evaluated"] == 0
    assert first["evaluations_pending"] == 1
    second = run_worker(lambda: _FakeConn(db), _config(max_passes=1), **kwargs)
    assert second["evaluated"] == 1
    assert second["qualified_dossiers"] == 1
    assert second["evaluations_pending"] == 0
    assert drives == [flaky, refused, flaky]  # blocked never re-driven


def test_checkpoint_failure_recovers_backlog_without_rework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery committed, checkpoint save fails: backlog re-saved, resumed."""
    import app.discovery_runner as runner_module

    db = _db()
    ids = (str(uuid4()), str(uuid4()))
    real_save = runner_module.checkpoint_in_transaction
    calls = {"n": 0}

    def _flaky(conn: Any, run_id: Any, checkpoint: Any, **kwargs: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:  # fail the post-discovery fold save
            raise DatabaseUnavailableError("checkpoint write failed")
        return real_save(conn, run_id, checkpoint, **kwargs)

    monkeypatch.setattr(runner_module, "checkpoint_in_transaction", _flaky)
    drives: list[str] = []

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        drives.append(pid)
        return {"outcome": "evaluated", "qualified": 0}

    kwargs: dict[str, Any] = {
        "discovery_fn": lambda cell, run_id="", limit=0: _Pass(2, ids),
        "counts_fn": lambda: {},
        "sleep_fn": lambda seconds: None,
        "now_fn": _no_sabbath,
        "evaluate_fn": _eval,
    }
    first = run_worker(lambda: _FakeConn(db), _config(max_passes=1), **kwargs)
    assert first["outcome"] == "aborted"
    assert first["reason"] == "checkpoint write failed"
    assert first["checkpoint_durable"] is True  # finish-path re-save
    assert drives == []  # nothing evaluated before the backlog was durable
    assert sorted(db["runs"][0]["checkpoint"]["backlog"]) == sorted(ids)
    second = run_worker(lambda: _FakeConn(db), _config(max_passes=1), **kwargs)
    assert second["evaluated"] == 2
    assert sorted(drives) == sorted(ids)


@needs_live_pg
def test_live_backlog_checkpoint_round_trip() -> None:
    """Backlog + evaluations survive a real discovery_runs JSONB round-trip."""
    import pathlib

    from app.properties_db import get_connection

    migration = (
        pathlib.Path(__file__).resolve().parent.parent
        / "migrations"
        / "015_simulation_discovery_pilot.sql"
    )
    ddl = migration.read_text(encoding="utf-8")
    marker = "CREATE TABLE IF NOT EXISTS discovery_runs"
    ddl = ddl[ddl.index(marker):]
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
        scope = f"live-backlog-{uuid4().hex[:12]}"
        pid = str(uuid4())
        claimed = claim_work_in_transaction(
            conn, scope=scope, policy="pilot",
            checkpoint={"cursor": 0, "backlog": [pid],
                        "evaluations": {pid: {"outcome": "error"}}},
            config={"max_passes": 1}, created_by="live-test",
            owner="live-w1",
        )
        assert claimed is not None
        assert checkpoint_in_transaction(
            conn, claimed["run_id"],
            {"cursor": 0, "backlog": [],
             "evaluations": {pid: {"outcome": "evaluated"}}},
            owner="live-w1",
        ) is True
        reread = latest_run_in_transaction(conn, scope)
        assert reread is not None
        assert reread["checkpoint"]["backlog"] == []
        assert reread["checkpoint"]["evaluations"][pid]["outcome"] == "evaluated"
    finally:
        conn.rollback()
        conn.close()

@needs_live_pg
def test_live_recovery_folds_committed_pass() -> None:
    """Real PG: crash between discovery commit and checkpoint loses nothing.

    A running row holds a pending pass whose decision events committed;
    resume folds exactly the created+COMMITTED candidates (skips and
    malformed rows ignored), drains them with accurate counters, and
    never re-folds on a later burst. Committed rows are deleted in
    cleanup (discovery_runs/agent_events carry no append-only trigger).
    """
    from app.agents.gabriel.service import DECISION_EVENT_TYPE, MUTATION_COMMITTED
    from app.discovery_runner import _fresh_checkpoint
    from app.observability.events import AgentEvent, AgentId, RetentionClass
    from app.observability.store import insert_event_in_transaction
    from app.properties_db import get_connection

    scope = f"live-recover-{uuid4().hex[:12]}"
    pid_a, pid_b = str(uuid4()), str(uuid4())
    discovery_run = f"live-pass-{uuid4().hex[:12]}"
    conn = get_connection()
    try:
        claimed = claim_work_in_transaction(
            conn, scope=scope, policy="pilot",
            checkpoint=_fresh_checkpoint(_cells()),
            config={}, created_by="live-test", owner="live-crashed",
        )
        assert claimed is not None
        checkpoint = dict(claimed["checkpoint"])
        checkpoint["pending_pass"] = {
            "seq": 0, "discovery_run_id": discovery_run,
            "cell": _cells()[0].key,
        }
        assert checkpoint_in_transaction(
            conn, claimed["run_id"], checkpoint, owner="live-crashed"
        ) is True
        decisions = [
            ("created", MUTATION_COMMITTED, pid_a),
            ("created", MUTATION_COMMITTED, pid_b),
            ("skipped_duplicate", MUTATION_COMMITTED, str(uuid4())),
            ("created", MUTATION_COMMITTED, "not-a-uuid"),
        ]
        for outcome, mutation, pid in decisions:
            insert_event_in_transaction(
                conn,
                AgentEvent(
                    agent=AgentId.GABRIEL, run_id=discovery_run,
                    event_type=DECISION_EVENT_TYPE, summary="live decision",
                    details={
                        "outcome": outcome, "mutation_status": mutation,
                        "result": {"property_id": pid},
                    },
                    retention_class=RetentionClass.PERMANENT,
                ),
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET heartbeat_at = now() - make_interval(mins := 5) "
                "WHERE run_id = %(run)s",
                {"run": claimed["run_id"]},
            )
        conn.commit()
    finally:
        conn.close()
    evaluated: list[str] = []

    def _evaluate(scope_name: str, pid: str) -> dict[str, Any]:
        evaluated.append(pid)
        return {"outcome": "evaluated", "qualified": 0}

    def _empty(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        return _Pass(0, ())

    try:
        report = run_worker(
            get_connection,
            _config(
                scope=scope, max_passes=2, max_created_total=50,
                max_evaluations_per_pass=5, resume=True,
                claim_lease_seconds=30.0, acquire_approved=True,
                created_by="live-test",
            ),
            discovery_fn=_empty, counts_fn=lambda: {},
            evaluate_fn=_evaluate, sleep_fn=lambda seconds: None,
            now_fn=_no_sabbath,
        )
        assert report["recovered_pending_pass"] == {
            "discovery_run_id": discovery_run, "enqueued": 2,
            "already_folded": False, "restored_created": 2,
            "restored_pass": True, "restored_request": True,
            "unit_key": _cells()[0].unit_key,
        }
        assert evaluated == [pid_a, pid_b]
        assert report["created"] == 0
        assert report["evaluated"] == 2
        check = get_connection()
        try:
            reread = latest_run_in_transaction(check, scope)
            assert reread is not None
            assert reread["checkpoint"]["backlog"] == []
            assert reread["checkpoint"]["created_total"] == 2
            assert reread["checkpoint"]["per_cell"][_cells()[0].key] == 2
            assert reread["checkpoint"]["cell_state"][
                _cells()[0].unit_key]["created"] == 2
            assert reread["checkpoint"]["evaluations"][pid_a]["outcome"] == "evaluated"
        finally:
            check.rollback()
            check.close()
        # A later burst re-folds nothing: terminal outcomes stay terminal.
        again = run_worker(
            get_connection,
            _config(
                scope=scope, max_passes=1, max_created_total=50,
                max_evaluations_per_pass=5, resume=True,
                claim_lease_seconds=30.0, acquire_approved=True,
                created_by="live-test",
            ),
            discovery_fn=_empty, counts_fn=lambda: {},
            evaluate_fn=_evaluate, sleep_fn=lambda seconds: None,
            now_fn=_no_sabbath,
        )
        assert again["recovered_pending_pass"] is None
        assert evaluated == [pid_a, pid_b]
    finally:
        cleanup = get_connection()
        try:
            with cleanup.cursor() as cur:
                cur.execute(
                    "DELETE FROM agent_events WHERE run_id = %(run)s",
                    {"run": discovery_run},
                )
                cur.execute(
                    "DELETE FROM discovery_runs WHERE scope = %(scope)s",
                    {"scope": scope},
                )
            cleanup.commit()
        finally:
            cleanup.close()


@needs_live_pg
def test_live_ceiling_drains_backlog_without_new_discovery() -> None:
    """Real PG: the creation ceiling stops discovery, not evaluations."""
    from app.discovery_runner import _fresh_checkpoint
    from app.properties_db import get_connection

    scope = f"live-ceiling-{uuid4().hex[:12]}"
    pids = [str(uuid4()), str(uuid4())]
    conn = get_connection()
    try:
        checkpoint = _fresh_checkpoint(_cells())
        checkpoint["created_total"] = 2
        checkpoint["backlog"] = list(pids)
        claimed = claim_work_in_transaction(
            conn, scope=scope, policy="pilot", checkpoint=checkpoint,
            config={}, created_by="live-test", owner="live-w1",
        )
        assert claimed is not None
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET heartbeat_at = now() - make_interval(mins := 5) "
                "WHERE run_id = %(run)s",
                {"run": claimed["run_id"]},
            )
        conn.commit()
    finally:
        conn.close()

    def _must_not_run(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        raise AssertionError("discovery must not run once the ceiling is hit")

    evaluated: list[str] = []

    def _evaluate(scope_name: str, pid: str) -> dict[str, Any]:
        evaluated.append(pid)
        return {"outcome": "evaluated", "qualified": 1}

    try:
        report = run_worker(
            get_connection,
            _config(
                scope=scope, max_passes=3, max_created_total=2,
                max_evaluations_per_pass=5, resume=True,
                claim_lease_seconds=30.0, acquire_approved=True,
                created_by="live-test",
            ),
            discovery_fn=_must_not_run, counts_fn=lambda: {},
            evaluate_fn=_evaluate, sleep_fn=lambda seconds: None,
            now_fn=_no_sabbath,
        )
        assert report["outcome"] == "completed"
        assert report["reason"] == "creation budget exhausted"
        assert report["created"] == 0
        assert sorted(evaluated) == sorted(pids)
        assert report["evaluated"] == 2
        assert report["qualified_dossiers"] == 2
        check = get_connection()
        try:
            reread = latest_run_in_transaction(check, scope)
            assert reread is not None
            assert reread["checkpoint"]["backlog"] == []
            assert reread["checkpoint"]["created_total"] == 2
        finally:
            check.rollback()
            check.close()
    finally:
        cleanup = get_connection()
        try:
            with cleanup.cursor() as cur:
                cur.execute(
                    "DELETE FROM discovery_runs WHERE scope = %(scope)s",
                    {"scope": scope},
                )
            cleanup.commit()
        finally:
            cleanup.close()


@needs_live_pg
def test_live_two_connections_worker_exclusion() -> None:
    """Real PG: one worker per scope across connections, fenced adoption."""
    import threading

    from app.discovery_runner import _fresh_checkpoint, finish_run_in_transaction
    from app.properties_db import get_connection

    scope = f"live-excl-{uuid4().hex[:12]}"
    conn1 = get_connection()
    conn2 = get_connection()
    try:
        first = claim_work_in_transaction(
            conn1, scope=scope, policy="pilot",
            checkpoint=_fresh_checkpoint(_cells()),
            config={}, created_by="live-test", owner="live-w1",
        )
        assert first is not None
        conn1.commit()
        assert claim_work_in_transaction(
            conn2, scope=scope, policy="pilot",
            checkpoint=_fresh_checkpoint(_cells()), owner="live-w2",
        ) is None
        assert request_stop_in_transaction(conn2, scope) is True
        conn2.commit()
        assert run_status_in_transaction(conn2, first["run_id"]) == "running"
        assert claim_work_in_transaction(
            conn2, scope=scope, policy="pilot",
            checkpoint=_fresh_checkpoint(_cells()), owner="live-w3",
        ) is None
        assert checkpoint_in_transaction(
            conn2, first["run_id"], {"cursor": 9}, owner="live-w2"
        ) is False
        # Stop-requested rows adopt only on explicit resume, clearing the flag.
        with conn1.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET heartbeat_at = now() - make_interval(mins := 5) "
                "WHERE run_id = %(run)s",
                {"run": first["run_id"]},
            )
        conn1.commit()
        assert adopt_stalled_in_transaction(
            conn2, first["run_id"], "live-w2", 30.0
        ) is None
        adopted = adopt_stalled_in_transaction(
            conn2, first["run_id"], "live-w2", 30.0, resume=True
        )
        assert adopted is not None and adopted["owner"] == "live-w2"
        assert adopted["stop_requested"] is False
        conn2.commit()
        assert checkpoint_in_transaction(
            conn1, first["run_id"], {"cursor": 10}, owner="live-w1"
        ) is False
        finish_run_in_transaction(conn2, first["run_id"], "completed")
        conn2.commit()
    finally:
        conn1.rollback()
        conn1.close()
        conn2.rollback()
        conn2.close()

    # Racing adopters serialize on the row lock: exactly one wins.
    race_scope = f"live-race-{uuid4().hex[:12]}"
    setup = get_connection()
    try:
        crashed = claim_work_in_transaction(
            setup, scope=race_scope, policy="pilot",
            checkpoint=_fresh_checkpoint(_cells()),
            config={}, created_by="live-test", owner="live-crashed",
        )
        assert crashed is not None
        with setup.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET heartbeat_at = now() - make_interval(mins := 5) "
                "WHERE run_id = %(run)s",
                {"run": crashed["run_id"]},
            )
        setup.commit()
        race_run = crashed["run_id"]
    finally:
        setup.close()
    barrier = threading.Barrier(2)
    winners: list[str] = []

    def _adopt(slot: str) -> None:
        worker_conn = get_connection()
        try:
            barrier.wait(timeout=30)
            row = adopt_stalled_in_transaction(
                worker_conn, race_run, f"live-{slot}", 30.0, resume=True
            )
            if row is not None:
                winners.append(slot)
            worker_conn.commit()
        finally:
            worker_conn.close()

    threads = [
        threading.Thread(target=_adopt, args=("r1",)),
        threading.Thread(target=_adopt, args=("r2",)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not [thread for thread in threads if thread.is_alive()]
        assert len(winners) == 1
    finally:
        cleanup = get_connection()
        try:
            with cleanup.cursor() as cur:
                cur.execute(
                    "DELETE FROM discovery_runs WHERE scope = ANY(%(scopes)s)",
                    {"scopes": [scope, race_scope]},
                )
            cleanup.commit()
        finally:
            cleanup.close()


def test_pass_attribution_saved_before_discovery() -> None:
    db = _db()
    seen: dict[str, Any] = {}

    def _discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        saved = db["runs"][0]["checkpoint"].get("pending_pass") or {}
        seen["pending"] = dict(saved)
        seen["run_id"] = run_id
        seen["cell"] = cell.key
        return _Pass(0, ())

    report = run_worker(
        lambda: _FakeConn(db), _config(max_passes=1),
        discovery_fn=_discovery, counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert seen["pending"]["discovery_run_id"] == seen["run_id"]
    assert seen["pending"]["cell"] == seen["cell"]
    assert seen["run_id"].startswith("runner:test:")
    assert len(seen["run_id"]) <= 128
    assert db["runs"][0]["checkpoint"]["pending_pass"] is None


def _created_event(pid: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        event_type="gabriel.discovery.decision",
        details={"outcome": "created", "mutation_status": "COMMITTED",
                 "result": {"property_id": pid}},
    )


def test_recover_pending_pass_enqueues_exact_created() -> None:
    from types import SimpleNamespace

    from app.discovery_runner import _fresh_checkpoint, _recover_pending_pass

    good1, good2 = str(uuid4()), str(uuid4())

    def _ev(outcome: str, pid: str, mutation: str = "COMMITTED",
            etype: str = "gabriel.discovery.decision") -> Any:
        return SimpleNamespace(
            event_type=etype,
            details={"outcome": outcome, "mutation_status": mutation,
                     "result": {"property_id": pid}},
        )

    events = [
        _ev("created", good1),
        _ev("skipped_duplicate", str(uuid4())),
        _ev("created", good2),
        _ev("created", good1),  # duplicate delivery
        _ev("created", "not-a-uuid"),
        _ev("created", good2, mutation="NOT_ATTEMPTED"),
        SimpleNamespace(event_type="gabriel.discovery.run",
                        details={"outcome": "run_completed"}),
        SimpleNamespace(event_type="gabriel.discovery.decision", details=None),
    ]
    checkpoint = _fresh_checkpoint(_cells())
    checkpoint["pending_pass"] = {
        "seq": 0, "discovery_run_id": "runner:t:r:0", "cell": "k",
    }
    queried: list[str] = []

    def _events(run_id: str) -> list[Any]:
        queried.append(run_id)
        return list(events) if run_id == "runner:t:r:0" else []

    note = _recover_pending_pass(checkpoint, events_fn=_events)
    assert note == {"discovery_run_id": "runner:t:r:0", "enqueued": 2,
                    "already_folded": False, "restored_created": 2,
                    "restored_pass": True, "restored_request": True,
                    "unit_key": ""}
    assert queried == ["runner:t:r:0"]  # exact run only, no fuzzy scan
    assert checkpoint["backlog"] == [good1, good2]
    assert checkpoint["pending_pass"] is None
    assert checkpoint["discovery_runs"] == ["runner:t:r:0"]
    assert checkpoint["created_total"] == 2  # totals restored...
    assert checkpoint["passes"] == 1
    assert checkpoint["overpass_requests"] == 1
    assert checkpoint["cell_state"] == {}  # ...but unknown cell "k" names no unit
    again = _recover_pending_pass(checkpoint, events_fn=_events)
    assert again == {"discovery_run_id": "", "enqueued": 0,
                     "already_folded": True}
    assert checkpoint["backlog"] == [good1, good2]  # idempotent


def test_recover_pending_pass_restores_creation_accounting() -> None:
    from app.discovery_runner import (
        _fresh_checkpoint,
        _recover_pending_pass,
        _unit_runnable,
    )

    cell = _cells()[0]
    good1, good2 = str(uuid4()), str(uuid4())
    checkpoint = _fresh_checkpoint(_cells())
    checkpoint["pending_pass"] = {
        "seq": 0,
        "discovery_run_id": "runner:t:r:0",
        "cell": cell.key,
        "unit": cell.unit_key,
    }

    def _events(run_id: str) -> list[Any]:
        assert run_id == "runner:t:r:0"
        return [_created_event(good1), _created_event(good2)]

    note = _recover_pending_pass(checkpoint, events_fn=_events)
    assert note["already_folded"] is False
    assert note["enqueued"] == 2
    assert note["restored_created"] == 2
    assert note["restored_pass"] is True
    assert note["restored_request"] is True
    assert note["unit_key"] == cell.unit_key
    assert checkpoint["backlog"] == [good1, good2]
    assert checkpoint["created_total"] == 2
    assert checkpoint["per_cell"][cell.key] == 2
    assert checkpoint["passes"] == 1
    assert checkpoint["overpass_requests"] == 1
    state = checkpoint["cell_state"][cell.unit_key]
    assert state["created"] == 2
    assert state["passes"] == 1
    # The finding's exact scenario: two committed creations under a
    # two-property unit limit must not permit a third after restart.
    assert _unit_runnable(
        cell, state, counts={}, policy="pilot", effective_limit=2,
        max_created_per_unit=2,
    ) is False
    # Recovery is idempotent: a second fold restores nothing more.
    again = _recover_pending_pass(checkpoint, events_fn=_events)
    assert again["already_folded"] is True
    assert checkpoint["created_total"] == 2
    assert checkpoint["cell_state"][cell.unit_key]["created"] == 2


def test_recover_pending_pass_skips_folded_without_query() -> None:
    from app.discovery_runner import _fresh_checkpoint, _recover_pending_pass

    checkpoint = _fresh_checkpoint(_cells())
    checkpoint["pending_pass"] = {
        "seq": 0, "discovery_run_id": "runner:t:r:0", "cell": "k",
    }
    checkpoint["discovery_runs"] = ["runner:t:r:0"]

    def _boom(run_id: str) -> list[Any]:
        raise AssertionError("folded passes must not re-query")

    note = _recover_pending_pass(checkpoint, events_fn=_boom)
    assert note["already_folded"] is True
    assert checkpoint["pending_pass"] is None


def test_resume_recovers_interrupted_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.discovery_runner import _fresh_checkpoint

    db = _db()
    pid = str(uuid4())
    checkpoint = _fresh_checkpoint(_cells())
    checkpoint["pending_pass"] = {
        "seq": 0, "discovery_run_id": "runner:test:aaa:0",
        "cell": _cells()[0].key,
    }
    db["runs"].append({
        "run_id": str(uuid4()), "scope": "test", "policy": "pilot",
        "status": "completed", "config": {}, "checkpoint": checkpoint,
        "usage": {}, "created_by": "t", "owner": "w0",
        "heartbeat_at": _now(), "stop_requested": False, "error": None,
        "started_at": "t0", "finished_at": "end",
    })
    queried: list[str] = []

    def _by_run(run_id: str, limit: int = 1000, config: Any = None) -> list[Any]:
        queried.append(run_id)
        return [_created_event(pid)]

    monkeypatch.setattr("app.observability.store.by_run", _by_run)
    drives: list[str] = []
    discovery_calls: list[str] = []

    def _discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        discovery_calls.append(cell.key)
        return _Pass(0, ())

    def _eval(scope: str, candidate: str) -> dict[str, Any]:
        drives.append(candidate)
        return {"outcome": "evaluated", "qualified": 0}

    # max_passes=2: the recovered pass's own request now correctly
    # consumes one request of the burst bound, leaving one live pass.
    report = run_worker(
        lambda: _FakeConn(db), _config(max_passes=2),
        discovery_fn=_discovery, counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    assert queried == ["runner:test:aaa:0"]
    assert report["recovered_pending_pass"] == {
        "discovery_run_id": "runner:test:aaa:0", "enqueued": 1,
        "already_folded": False, "restored_created": 1,
        "restored_pass": True, "restored_request": True,
        "unit_key": _cells()[0].unit_key,
    }
    assert report["evaluated"] == 1 and drives == [pid]
    assert len(discovery_calls) == 1  # recovery itself made no discovery call


def test_resume_unit_cap_survives_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after two capped-unit creations must not permit a third."""
    from app.discovery_runner import _fresh_checkpoint

    cell = _cells()[0]
    db = _db()
    pid1, pid2 = str(uuid4()), str(uuid4())
    checkpoint = _fresh_checkpoint(_cells())
    checkpoint["pending_pass"] = {
        "seq": 0,
        "discovery_run_id": "runner:test:aaa:0",
        "cell": cell.key,
        "unit": cell.unit_key,
    }
    db["runs"].append({
        "run_id": str(uuid4()), "scope": "test", "policy": "pilot",
        "status": "completed", "config": {}, "checkpoint": checkpoint,
        "usage": {}, "created_by": "t", "owner": "w0",
        "heartbeat_at": _now(), "stop_requested": False, "error": None,
        "started_at": "t0", "finished_at": "end",
    })

    def _by_run(run_id: str, limit: int = 1000, config: Any = None) -> list[Any]:
        assert run_id == "runner:test:aaa:0"
        return [_created_event(pid1), _created_event(pid2)]

    monkeypatch.setattr("app.observability.store.by_run", _by_run)
    discovery_calls: list[str] = []

    def _discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        discovery_calls.append(cell.key)
        return _Pass(0, ())

    drives: list[str] = []

    def _eval(scope: str, candidate: str) -> dict[str, Any]:
        drives.append(candidate)
        return {"outcome": "evaluated", "qualified": 0}

    report = run_worker(
        lambda: _FakeConn(db), _config(max_passes=4, max_created_per_unit=2),
        discovery_fn=_discovery, counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    assert report["recovered_pending_pass"]["restored_created"] == 2
    assert report["outcome"] == "completed"
    assert cell.key not in discovery_calls  # capped unit never rediscovered
    assert sorted(drives) == sorted([pid1, pid2])
    final = db["runs"][-1]["checkpoint"]
    assert final["created_total"] == 2
    assert final["cell_state"][cell.unit_key]["created"] == 2


def test_failed_pass_folds_partial_creations_without_seq_reuse() -> None:
    """A raising discovery call keeps its committed work and spends its seq."""
    db = _db()
    pid = str(uuid4())
    store: dict[str, list[Any]] = {}
    calls: list[tuple[str, str]] = []
    failed_once: list[bool] = []

    def _discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        calls.append((cell.key, run_id))
        if not failed_once:
            failed_once.append(True)
            store.setdefault(run_id, []).append(_created_event(pid))
            raise RuntimeError("provider boom after partial commit")
        return _Pass(0, ())

    drives: list[str] = []

    def _eval(scope: str, candidate: str) -> dict[str, Any]:
        drives.append(candidate)
        return {"outcome": "evaluated", "qualified": 0}

    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=4, max_consecutive_failures=5),
        discovery_fn=_discovery, counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval, events_fn=lambda run_id: list(store.get(run_id, [])),
    )
    assert drives == [pid]  # partial commit survived the failure
    assert report["outcome"] == "completed"
    run_ids = [run_id for _, run_id in calls]
    assert len(set(run_ids)) == len(run_ids)  # failed seq never reused
    final = db["runs"][-1]["checkpoint"]
    assert final["created_total"] == 1
    assert final["overpass_requests"] == len(calls)
    first_unit = _cells()[0].unit_key
    assert final["cell_state"][first_unit]["created"] == 1
    # Folded failure plus one legitimate retry that found nothing
    # (retry-after-failure stays allowed; F6 caps attempts per unit).
    assert final["cell_state"][first_unit]["passes"] == 2


def test_recovery_unavailable_aborts_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.discovery_runner import _fresh_checkpoint

    db = _db()
    checkpoint = _fresh_checkpoint(_cells())
    checkpoint["pending_pass"] = {
        "seq": 0, "discovery_run_id": "runner:test:aaa:0", "cell": "k",
    }
    db["runs"].append({
        "run_id": str(uuid4()), "scope": "test", "policy": "pilot",
        "status": "completed", "config": {}, "checkpoint": checkpoint,
        "usage": {}, "created_by": "t", "owner": "w0",
        "heartbeat_at": _now(), "stop_requested": False, "error": None,
        "started_at": "t0", "finished_at": "end",
    })

    def _down(run_id: str, limit: int = 1000, config: Any = None) -> list[Any]:
        raise DatabaseUnavailableError("event store down")

    monkeypatch.setattr("app.observability.store.by_run", _down)
    report = run_worker(
        lambda: _FakeConn(db), _config(max_passes=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0, ()),
        counts_fn=lambda: {}, sleep_fn=lambda seconds: None,
        now_fn=_no_sabbath,
    )
    assert report["outcome"] == "aborted"
    assert report["reason"] == "pending-pass recovery unavailable"


def test_creation_ceiling_drains_backlog_without_new_discovery() -> None:
    db = _db()
    ids = tuple(str(uuid4()) for _ in range(10))
    drives: list[str] = []

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        drives.append(pid)
        return {"outcome": "evaluated", "qualified": 0}

    kwargs: dict[str, Any] = {
        "counts_fn": lambda: {},
        "sleep_fn": lambda seconds: None,
        "now_fn": _no_sabbath,
        "evaluate_fn": _eval,
    }
    first = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, max_evaluations_per_pass=5,
                max_created_total=10),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(10, ids),
        **kwargs,
    )
    assert first["evaluated"] == 5
    assert first["evaluations_pending"] == 5

    def _no_discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        raise AssertionError("ceiling must prevent new discovery")

    second = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=2, max_evaluations_per_pass=5,
                max_created_total=10),
        discovery_fn=_no_discovery,
        **kwargs,
    )
    assert second["outcome"] == "completed"
    assert second["reason"] == "creation budget exhausted"
    assert second["created"] == 0
    assert second["evaluated"] == 5
    assert second["evaluations_pending"] == 0
    assert sorted(drives) == sorted(ids)


def test_drain_rounds_observe_stop_requests() -> None:
    db = _db()
    ids = tuple(str(uuid4()) for _ in range(4))

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        if len([d for d in db["runs"][-1]["checkpoint"]["evaluations"]]) >= 1:
            db["runs"][-1]["stop_requested"] = True
        return {"outcome": "evaluated", "qualified": 0}

    kwargs: dict[str, Any] = {
        "counts_fn": lambda: {},
        "sleep_fn": lambda seconds: None,
        "now_fn": _no_sabbath,
        "evaluate_fn": _eval,
    }
    run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, max_evaluations_per_pass=1,
                max_created_total=4),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(4, ids),
        **kwargs,
    )
    second = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=5, max_evaluations_per_pass=2,
                max_created_total=4),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0, ()),
        **kwargs,
    )
    assert second["outcome"] == "paused"
    assert second["reason"] == "operator stop observed"
    assert second["evaluated"] == 1  # stop observed between evaluations
    assert second["evaluations_pending"] == 2


def test_quota_satisfied_drains_backlog() -> None:
    from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER
    from app.pilot import PILOT_CATEGORIES

    db = _db()
    ids = (str(uuid4()), str(uuid4()))
    drives: list[str] = []

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        drives.append(pid)
        return {"outcome": "evaluated", "qualified": 0}

    run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=1, max_evaluations_per_pass=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(2, ids),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    counts = {
        (county, category): 4
        for county in COUNTY_PRIORITY_ORDER
        for category in PILOT_CATEGORIES
    }

    def _no_discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        raise AssertionError("quota must prevent new discovery")

    report = run_worker(
        lambda: _FakeConn(db),
        _config(max_passes=2, max_evaluations_per_pass=5),
        discovery_fn=_no_discovery,
        counts_fn=lambda: dict(counts),
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    assert report["outcome"] == "completed"
    assert report["reason"] == "pilot quota satisfied"
    assert report["evaluated"] == 1
    assert sorted(drives) == sorted(ids)


def test_zero_created_pass_exhausts_unit() -> None:
    db = _db()
    calls: list[tuple[str, int]] = []

    def _seen(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        calls.append((cell.unit_key, limit))
        return _Pass(0, ())

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=4),
        discovery_fn=_seen,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert report["reason"] == "all configured work units exhausted"
    assert len(calls) == 1  # the exhausted query is never repeated
    assert report["exhausted_units"] == [calls[0][0]]
    assert report["overpass_requests"] == 1


def test_exhausted_unit_reopens_on_wider_limit() -> None:
    db = _db()
    seen: list[int] = []

    def _seen(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        seen.append(limit)
        return _Pass(0, ())

    kwargs: dict[str, Any] = {
        "counts_fn": lambda: {}, "sleep_fn": lambda seconds: None,
        "now_fn": _no_sabbath,
    }
    first = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=4, pass_limit=5),
        discovery_fn=_seen, **kwargs,
    )
    assert first["reason"] == "all configured work units exhausted"
    second = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=4, pass_limit=10, resume=True),
        discovery_fn=_seen, **kwargs,
    )
    assert seen == [5, 10]  # wider window reopens the unit across bursts
    assert second["reason"] == "all configured work units exhausted"


def test_fulfilled_cell_skipped_pilot() -> None:
    db = _db()

    def _must_not_run(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        raise AssertionError("fulfilled pilot cell must be skipped")

    counts = {("dallas county", "high_rise"): 4}
    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=4),
        discovery_fn=_must_not_run,
        counts_fn=lambda: dict(counts),
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert report["reason"] == "all configured work units fulfilled"
    assert report["overpass_requests"] == 0


def test_timed_ignores_fulfillment() -> None:
    db = _db()
    calls: list[str] = []

    def _seen(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        calls.append(cell.key)
        return _Pass(0, ())

    counts = {("dallas county", "high_rise"): 4}
    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=4, policy="timed"),
        discovery_fn=_seen,
        counts_fn=lambda: dict(counts),
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert len(calls) == 1  # attempted despite fulfillment, then exhausted
    assert report["reason"] == "all configured work units exhausted"


def test_pass_limit_clamped_to_remaining_allowance() -> None:
    db = _db()
    seen: list[int] = []

    def _honest(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        seen.append(limit)
        ids = tuple(str(uuid4()) for _ in range(min(limit, 2)))
        return _Pass(len(ids), ids)

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=4, pass_limit=10, max_created_total=3),
        discovery_fn=_honest,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert seen == [3, 1]  # min(pass_limit, remaining) per pass
    assert report["created"] == 3
    assert report["reason"] == "creation budget exhausted"


def test_overpass_budget_gates_discovery() -> None:
    db = _db()
    calls: list[str] = []

    def _seen(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        calls.append(cell.key)
        return _Pass(1, (str(uuid4()),))

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=5, max_overpass_requests=1),
        discovery_fn=_seen,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert len(calls) == 1
    assert report["overpass_requests"] == 1
    assert report["outcome"] == "completed"
    assert report["reason"] == "overpass request budget exhausted"


def test_overpass_retries_share_request_budget() -> None:
    db = _db()
    calls: list[str] = []

    def _flaky(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        calls.append(cell.key)
        raise RuntimeError("provider down")

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=5, max_overpass_requests=2),
        discovery_fn=_flaky,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert len(calls) == 2  # failed attempts spend the budget too
    assert report["overpass_requests"] == 2
    assert report["reason"] == "overpass request budget exhausted"


def test_brave_bound_halts_evaluations() -> None:
    db = _db()

    def _source(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        ids = (str(uuid4()), str(uuid4()))
        return _Pass(2, ids)

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        # Spend above the configured per-run limit simulates a resumed
        # run keeping its wider persisted limit: the backstop case.
        return {"outcome": "evaluated", "qualified": 0,
                "decision": "needs_evidence", "brave_attempts": 2}

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=3, max_evaluations_per_pass=2,
                evaluation_attempt_limit=1),
        discovery_fn=_source,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    assert report["request_bounds"]["brave_requests"] == 3 * 2 * 1
    assert report["brave_requests"] == 6
    assert report["evaluated"] == 3
    assert report["evaluations_preempted"] == "brave request bound reached"
    assert report["unresolved"]["queued_backlog"] == 3


def test_report_states_cells_deficits_and_decisions() -> None:
    db = _db()
    pid = str(uuid4())

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        return {"outcome": "evaluated", "qualified": 1, "decision": "needs_evidence"}

    counts = {("dallas county", "high_rise"): 2}
    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(1, (pid,)),
        counts_fn=lambda: dict(counts),
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    assert len(report["cells"]) == 36
    dallas = next(
        row for row in report["cells"]
        if (row["county"], row["category"]) == ("dallas county", "high_rise")
    )
    assert (dallas["credited"], dallas["target"], dallas["deficit"]) == (2, 4, 2)
    assert report["remaining_quota"] == 35 * 4 + 2
    assert report["raphael_decisions"] == {"needs_evidence": 1}
    assert report["credited_new"] == 0  # static counts never double-count
    assert report["request_bounds"] == {
        "overpass_requests": 1, "brave_requests": 45, "census_requests": 0,
    }


def test_credited_new_counts_per_cell_gains() -> None:
    db = _db()
    staged = [{}, {("dallas county", "high_rise"): 1}]
    position = 0

    def _counts() -> dict[tuple[str, str], int]:
        nonlocal position
        out = staged[min(position, 1)]
        position += 1
        return dict(out)

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=2),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(0, ()),
        counts_fn=_counts,
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["credited_new"] == 1
    assert report["remaining_quota"] == 143


def test_duration_checked_between_evaluations() -> None:
    db = _db()
    ticks = [0.0, 0.0, 0.0, 1e6]
    position = 0

    def _clock() -> float:
        nonlocal position
        value = ticks[min(position, len(ticks) - 1)]
        position += 1
        return value

    ids = (str(uuid4()), str(uuid4()))

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        return {"outcome": "evaluated", "qualified": 0}

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=4, max_duration_seconds=60.0),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(2, ids),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
        clock_fn=_clock,
    )
    assert report["outcome"] == "paused"
    assert report["reason"] == "duration limit reached"
    assert report["evaluated"] == 1
    assert report["evaluations_pending"] == 1


def test_runner_config_rejects_unknown_cells_and_windows() -> None:
    good = RunnerCell(county="dallas county", category="high_rise",
                      bbox=(32.0, -97.0, 33.0, -96.0))

    def _check(cell: RunnerCell, **overrides: Any) -> None:
        params: dict[str, Any] = {
            "scope": "t", "policy": "pilot", "cells": [cell],
            "max_passes": 1, "max_duration_seconds": 60.0,
            "max_created_total": 10,
        }
        params.update(overrides)
        with pytest.raises(RunnerError):
            RunnerConfig(**params).validate()

    _check(RunnerCell(county="atlantis county", category="high_rise", bbox=good.bbox))
    _check(RunnerCell(county="dallas county", category="skyscraper", bbox=good.bbox))
    _check(RunnerCell(county="dallas county", category="high_rise",
                       bbox=(33.0, -97.0, 32.0, -96.0)))
    _check(RunnerCell(county="dallas county", category="high_rise",
                       bbox=(float("nan"), -97.0, 33.0, -96.0)))
    _check(RunnerCell(county="dallas county", category="high_rise",
                       bbox=good.bbox, source=""))
    _check(good, max_overpass_requests=0)
    RunnerConfig(scope="t", policy="pilot", cells=[good], max_passes=1,
                 max_duration_seconds=60.0, max_created_total=10).validate()


def test_unit_keys_distinct_for_subdivisions() -> None:
    north = RunnerCell(county="dallas county", category="high_rise",
                       bbox=(32.0, -97.0, 33.0, -96.0))
    south = RunnerCell(county="dallas county", category="high_rise",
                       bbox=(32.5, -96.9, 33.0, -96.5))
    assert north.key == south.key
    assert north.unit_key != south.unit_key
    db = _db()
    calls: list[str] = []

    def _seen(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        calls.append(cell.unit_key)
        if cell.unit_key == north.unit_key:
            return _Pass(0, ())
        return _Pass(1, (str(uuid4()),))

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[north, south], max_passes=4),
        discovery_fn=_seen,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert calls[0] == north.unit_key
    assert calls[1] == south.unit_key  # exhaustion is per-unit, not per-cell
    assert report["exhausted_units"] == [north.unit_key]


def test_brave_usage_zero_without_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import DatabaseNotConfiguredError
    from app.discovery_runner import _brave_used_for

    def _missing() -> Any:
        raise DatabaseNotConfiguredError("no database configured")

    monkeypatch.setattr("app.properties_db.get_connection", _missing)
    assert _brave_used_for("runner:s:pid", "q") == 0


def test_brave_usage_reads_run_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.discovery_runner import _brave_used_for

    monkeypatch.setattr(
        "app.properties_db.get_connection", lambda: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setattr(
        "app.workflow_db.get_run_by_key_in_transaction",
        lambda conn, key: SimpleNamespace(usage={"brave_attempts": 3}),
    )
    assert _brave_used_for("runner:s:pid", "q") == 3


def test_final_report_reads_fresh_counts_after_last_round() -> None:
    db = _db()
    staged = [{}, {("dallas county", "high_rise"): 1}]
    position = 0

    def _counts() -> dict[tuple[str, str], int]:
        nonlocal position
        out = staged[min(position, 1)]
        position += 1
        return dict(out)

    pid = str(uuid4())

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        return {"outcome": "evaluated", "qualified": 1, "decision": "needs_evidence"}

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(1, (pid,)),
        counts_fn=_counts,
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    # The pass budget fills immediately after the last evaluation
    # round; the final report must still include its credit.
    assert report["outcome"] == "completed"
    assert report["reason"] == "pass budget exhausted"
    assert report["evaluated"] == 1
    assert report["credited_new"] == 1
    assert report["remaining_quota"] == 143
    dallas = next(
        row for row in report["cells"]
        if (row["county"], row["category"]) == ("dallas county", "high_rise")
    )
    assert (dallas["credited"], dallas["deficit"]) == (1, 3)


def test_final_report_marks_quota_unavailable_on_read_failure() -> None:
    db = _db()
    calls = 0

    def _counts() -> dict[tuple[str, str], int]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("quota store down")
        return {}

    pid = str(uuid4())
    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=1),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(1, (pid,)),
        counts_fn=_counts,
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["outcome"] == "completed"
    assert report["cells"] == []
    assert report["remaining_quota"] is None
    assert report["credited_new"] is None
    assert report["created"] == 1  # committed work preserved
    assert report["checkpoint_durable"] is True


def test_per_unit_creation_cap_skips_capped_units() -> None:
    north = RunnerCell(county="dallas county", category="high_rise",
                       bbox=(32.0, -97.0, 33.0, -96.0))
    south = RunnerCell(county="dallas county", category="high_rise",
                       bbox=(32.5, -96.9, 33.0, -96.5))
    db = _db()
    calls: list[str] = []

    def _seen(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        calls.append(cell.unit_key)
        return _Pass(1, (str(uuid4()),))

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[north, south], max_passes=4, max_created_per_unit=1),
        discovery_fn=_seen,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert calls == [north.unit_key, south.unit_key]
    assert report["created"] == 2
    assert report["reason"] == "all configured work units at creation cap"


def test_per_unit_cap_clamps_pass_limit() -> None:
    db = _db()
    seen: list[int] = []

    def _honest(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        seen.append(limit)
        ids = tuple(str(uuid4()) for _ in range(limit))
        return _Pass(len(ids), ids)

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=4, pass_limit=10,
                max_created_per_unit=2),
        discovery_fn=_honest,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert seen == [2]  # min(pass_limit, total remaining, unit remaining)
    assert report["created"] == 2
    assert report["reason"] == "all configured work units at creation cap"


def test_no_runnable_reason_reports_capped_bucket() -> None:
    north = RunnerCell(county="dallas county", category="high_rise",
                       bbox=(32.0, -97.0, 33.0, -96.0))
    south = RunnerCell(county="dallas county", category="high_rise",
                       bbox=(32.5, -96.9, 33.0, -96.5))
    db = _db()

    def _seen(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        if cell.unit_key == north.unit_key:
            return _Pass(0, ())
        return _Pass(1, (str(uuid4()),))

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[north, south], max_passes=4, max_created_per_unit=1),
        discovery_fn=_seen,
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert report["reason"] == (
        "no runnable discovery units (1 exhausted, 0 fulfilled, "
        "1 at creation cap, 0 attempts spent)"
    )


def test_census_spend_accrues_from_outcomes() -> None:
    db = _db()
    ids = (str(uuid4()), str(uuid4()))

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        return {"outcome": "evaluated", "qualified": 0, "census_attempts": 1}

    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=1, max_evaluations_per_pass=2,
                max_census_requests=6),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(2, ids),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    assert report["census_requests"] == 2
    assert report["census_preempted"] is None
    assert report["request_bounds"]["census_requests"] == 6


def test_census_allowed_for_eval_gate() -> None:
    from app.discovery_runner import _census_allowed_for_eval

    open_config = _config(max_census_requests=6)
    assert _census_allowed_for_eval(open_config, 0) is True
    assert _census_allowed_for_eval(open_config, 5) is True
    assert _census_allowed_for_eval(open_config, 6) is False
    assert _census_allowed_for_eval(_config(), 0) is False  # disabled by default
    assert _census_allowed_for_eval(_config(max_census_requests=6,
                                            census_attempt_limit=0), 0) is False


def test_census_request_bounds_default_zero() -> None:
    from app.discovery_runner import burst_request_bounds

    assert burst_request_bounds(_config())["census_requests"] == 0
    assert burst_request_bounds(_config(max_census_requests=6))["census_requests"] == 6


def test_envelope_bounds_validated() -> None:
    good = RunnerCell(county="dallas county", category="high_rise",
                      bbox=(32.0, -97.0, 33.0, -96.0))

    def _check(**overrides: Any) -> None:
        params: dict[str, Any] = {
            "scope": "t", "policy": "pilot", "cells": [good],
            "max_passes": 1, "max_duration_seconds": 60.0,
            "max_created_total": 10,
        }
        params.update(overrides)
        with pytest.raises(RunnerError):
            RunnerConfig(**params).validate()

    _check(max_created_per_unit=0)
    _check(max_created_per_unit=True)
    _check(max_census_requests=0)
    _check(max_census_requests=-2)
    _check(census_attempt_limit=2)
    _check(census_attempt_limit=-1)
    _check(census_attempt_limit=True)
    RunnerConfig(scope="t", policy="pilot", cells=[good], max_passes=1,
                 max_duration_seconds=60.0, max_created_total=10,
                 max_created_per_unit=2, max_census_requests=6,
                 census_attempt_limit=1).validate()


def test_census_gate_skips_past_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db()
    seen: list[int] = []

    def _faithful(scope: str, pid: str, **kwargs: Any) -> dict[str, Any]:
        limit = int(kwargs.get("census_attempt_limit", 0))
        seen.append(limit)
        assert kwargs.get("census_principal") == "runner:test"
        return {"outcome": "evaluated", "qualified": 0,
                "census_attempts": 1 if limit else 0}

    monkeypatch.setattr("app.discovery_runner.evaluate_property", _faithful)
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    ids = (str(uuid4()), str(uuid4()))
    report = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=1, max_evaluations_per_pass=2,
                max_census_requests=1, acquire_approved=True),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(2, ids),
        counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
    )
    assert seen == [1, 0]  # second evaluation runs without county admission
    assert report["census_requests"] == 1
    assert report["census_preempted"] == "census request bound reached"
    assert report["evaluated"] == 2


def test_cli_preview_states_envelope_without_work(
    tmp_path: Any, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from app.discovery_runner import main

    cells_file = tmp_path / "cells.json"
    cells_file.write_text(json.dumps([{
        "county": "dallas county", "category": "high_rise",
        "bbox": [32.77, -96.81, 32.79, -96.785], "source": "overpass",
    }]))

    def _must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("preview must not execute")

    monkeypatch.setattr("app.discovery_runner.run_worker", _must_not_run)
    code = main([
        "preview", "--scope", "s", "--cells", str(cells_file),
        "--max-passes", "3", "--max-created", "6", "--max-created-per-unit", "2",
        "--max-evaluations", "2", "--evaluation-attempt-limit", "1",
        "--max-overpass-requests", "3", "--max-census-requests", "6",
    ])
    assert code == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["request_bounds"] == {
        "overpass_requests": 3, "brave_requests": 6, "census_requests": 6,
    }
    assert preview["max_created_per_unit"] == 2
    assert preview["census_attempt_limit"] == 1
    assert len(preview["units"]) == 1


def test_cli_run_parses_envelope_flags(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from app.discovery_runner import main

    cells_file = tmp_path / "cells.json"
    cells_file.write_text(json.dumps([{
        "county": "dallas county", "category": "high_rise",
        "bbox": [32.77, -96.81, 32.79, -96.785],
    }]))
    captured: dict[str, Any] = {}

    def _capture(connect_fn: Any, config: Any) -> dict[str, Any]:
        captured["config"] = config
        return {"outcome": "completed"}

    monkeypatch.setattr("app.discovery_runner.run_worker", _capture)
    code = main([
        "run", "--scope", "s", "--cells", str(cells_file),
        "--max-census-requests", "6", "--census-attempt-limit", "1",
        "--max-created-per-unit", "2",
    ])
    assert code == 0
    assert captured["config"].max_census_requests == 6
    assert captured["config"].census_attempt_limit == 1
    assert captured["config"].max_created_per_unit == 2


def test_dallas_opening_units_example_loads() -> None:
    from pathlib import Path

    from app.discovery_runner import _cells_from_json

    path = Path(__file__).resolve().parent.parent / "config" / "dallas_opening_units.example.json"
    cells = _cells_from_json(str(path))
    assert len(cells) == 3
    assert {cell.category for cell in cells} == {
        "high_rise", "commercial_property", "warehouse_industrial",
    }
    assert {cell.county for cell in cells} == {"dallas county"}
    assert {cell.source for cell in cells} == {"dcad_bulk"}
    for cell in cells:
        assert cell.bbox == (32.770, -96.810, 32.790, -96.785)
    RunnerConfig(scope="t", policy="pilot", cells=cells, max_passes=3,
                 max_duration_seconds=600.0, max_created_total=6).validate()


def _dallas_envelope_path() -> str:
    from pathlib import Path

    return str(
        Path(__file__).resolve().parent.parent
        / "config"
        / "dallas_opening_envelope.example.json"
    )


def test_dallas_opening_envelope_example_loads() -> None:
    from app.discovery_runner import envelope_config_from_json

    config = envelope_config_from_json(_dallas_envelope_path(), scope="pilot-dfw")
    assert config.scope == "pilot-dfw"
    assert config.policy == "pilot"
    assert len(config.cells) == 3
    assert {cell.category for cell in config.cells} == {
        "high_rise", "commercial_property", "warehouse_industrial",
    }
    assert {cell.county for cell in config.cells} == {"dallas county"}
    assert {cell.source for cell in config.cells} == {"dcad_bulk"}
    for cell in config.cells:
        assert cell.bbox == (32.770, -96.810, 32.790, -96.785)
    assert config.max_passes == 3
    assert config.max_created_total == 6
    assert config.max_created_per_unit == 2
    assert config.max_discovery_attempts_per_unit == 1
    assert config.pass_limit == 2
    assert config.pass_timeout_seconds == 12.0
    assert config.max_evaluations_per_pass == 2
    assert config.evaluation_attempt_limit == 1
    assert config.evaluation_timeout_seconds == 12.0
    assert config.max_overpass_requests == 3
    assert config.max_census_requests == 6
    assert config.census_attempt_limit == 1
    config.validate()


def test_envelope_loader_rejects_extras_and_live_execution(
    tmp_path: Any,
) -> None:
    import json

    from app.discovery_runner import RunnerError, envelope_config_from_json

    base: dict[str, Any] = {
        "envelope": "t", "policy": "pilot",
        "units": [{
            "county": "dallas county", "category": "high_rise",
            "bbox": [32.77, -96.81, 32.79, -96.785], "source": "overpass",
        }],
        "max_passes": 3, "max_duration_seconds": 1800,
        "max_created_total": 6, "max_created_per_unit": 2,
        "max_discovery_attempts_per_unit": 1, "pass_limit": 2,
        "pass_timeout_seconds": 12.0, "max_evaluations_per_pass": 2,
        "evaluation_attempt_limit": 1, "evaluation_timeout_seconds": 12.0,
        "max_overpass_requests": 3, "max_census_requests": 6,
        "census_attempt_limit": 1, "max_consecutive_failures": 3,
        "pacing_seconds": 5.0, "backoff_cap_seconds": 60.0,
        "claim_lease_seconds": 900.0,
        "meta_calls": 0, "outreach": "disabled",
    }

    def _load(extra: dict[str, Any]) -> Any:
        payload = dict(base)
        payload.update(extra)
        path = tmp_path / "envelope.json"
        path.write_text(json.dumps(payload))
        return envelope_config_from_json(str(path), scope="t")

    _load({})  # the complete shape loads
    with pytest.raises(RunnerError, match="unknown envelope key"):
        _load({"launch_now": True})
    with pytest.raises(RunnerError, match="meta_calls"):
        _load({"meta_calls": 1})
    with pytest.raises(RunnerError, match="outreach"):
        _load({"outreach": "enabled"})
    with pytest.raises(RunnerError, match="units"):
        _load({"units": []})


def test_unit_runnable_enforces_attempt_cap() -> None:
    from app.discovery_runner import _fresh_checkpoint, _unit_runnable, _unit_state

    cell = _cells()[0]
    checkpoint = _fresh_checkpoint(_cells())
    state = _unit_state(checkpoint, cell.unit_key)
    assert _unit_runnable(
        cell, state, counts={}, policy="pilot", effective_limit=2,
        max_discovery_attempts_per_unit=1,
    ) is True
    state["passes"] = 1  # the one allowed attempt is spent
    assert _unit_runnable(
        cell, state, counts={}, policy="pilot", effective_limit=2,
        max_discovery_attempts_per_unit=1,
    ) is False
    # A wider window reopens exhaustion but never the attempt cap.
    state["exhausted"] = True
    state["reason"] = "empty window"
    state["limit"] = 2
    assert _unit_runnable(
        cell, state, counts={}, policy="pilot", effective_limit=99,
        max_discovery_attempts_per_unit=1,
    ) is False
    assert _unit_runnable(
        cell, state, counts={}, policy="pilot", effective_limit=99,
    ) is True


def test_census_spend_resumes_across_bursts() -> None:
    db = _db()

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        return {"outcome": "evaluated", "qualified": 0, "census_attempts": 1}

    kwargs: dict[str, Any] = {
        "counts_fn": lambda: {}, "sleep_fn": lambda seconds: None,
        "now_fn": _no_sabbath, "evaluate_fn": _eval,
    }
    first = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=1, max_evaluations_per_pass=1,
                max_census_requests=6),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(1, (str(uuid4()),)),
        **kwargs,
    )
    assert first["census_requests"] == 1
    second = run_worker(
        lambda: _FakeConn(db),
        _config(cells=[_cells()[0]], max_passes=1, max_evaluations_per_pass=1,
                max_census_requests=6, resume=True),
        discovery_fn=lambda cell, run_id="", limit=0: _Pass(1, (str(uuid4()),)),
        **kwargs,
    )
    assert second["census_requests"] == 1  # burst delta against restored spend


def test_dallas_envelope_mocked_compliance() -> None:
    """The shipped Dallas envelope enforces every bound with mocked providers."""
    from app.discovery_runner import envelope_config_from_json

    db = _db()
    config = envelope_config_from_json(_dallas_envelope_path(), scope="pilot-dfw")
    calls: list[str] = []
    per_unit: dict[str, int] = {}

    def _discovery(cell: RunnerCell, run_id: str = "", limit: int = 0) -> _Pass:
        calls.append(cell.category)
        per_unit[cell.unit_key] = per_unit.get(cell.unit_key, 0) + 1
        if cell.category == "warehouse_industrial":
            return _Pass(0, ())  # legitimately absent in this window
        assert limit <= 2
        ids = tuple(str(uuid4()) for _ in range(2))
        return _Pass(2, ids)

    spent: dict[str, int] = {"brave": 0, "census": 0}

    def _eval(scope: str, pid: str) -> dict[str, Any]:
        # Retained DCAD records are inspected without a Brave search.
        spent["census"] += 1
        return {
            "outcome": "evaluated", "qualified": 0,
            "brave_attempts": 0, "census_attempts": 1,
        }

    report = run_worker(
        lambda: _FakeConn(db), config,
        discovery_fn=_discovery, counts_fn=lambda: {},
        sleep_fn=lambda seconds: None, now_fn=_no_sabbath,
        evaluate_fn=_eval,
    )
    assert report["outcome"] == "completed"
    assert report["overpass_requests"] == 0
    assert report["local_discovery_passes"] == 3  # one local read per unit
    assert report["created"] == 4  # H:2 + C:2 + W:0, within the 6 total
    assert report["brave_requests"] == 0
    assert report["census_requests"] == 4
    assert len(calls) == 3
    assert sorted(calls) == [
        "commercial_property", "high_rise", "warehouse_industrial",
    ]
    assert all(count == 1 for count in per_unit.values())
    units = {entry["cell"]: entry for entry in report["units"]}
    assert set(units) == {
        "dallas county||high_rise",
        "dallas county||commercial_property",
        "dallas county||warehouse_industrial",
    }
    for entry in units.values():
        assert entry["attempts"] == 1  # no unit skipped or repeated
    assert units["dallas county||high_rise"]["created"] == 2
    assert units["dallas county||commercial_property"]["created"] == 2
    assert units["dallas county||warehouse_industrial"]["created"] == 0


def test_cli_preview_and_run_share_envelope(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from app.discovery_runner import main

    def _must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("preview must not execute")

    monkeypatch.setattr("app.discovery_runner.run_worker", _must_not_run)
    code = main([
        "preview", "--scope", "pilot-dfw",
        "--envelope", _dallas_envelope_path(),
    ])
    assert code == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["request_bounds"] == {
        "overpass_requests": 0, "brave_requests": 0, "census_requests": 6,
    }
    assert preview["max_created_total"] == 6
    assert preview["max_created_per_unit"] == 2
    assert preview["max_discovery_attempts_per_unit"] == 1
    assert preview["evaluation_attempt_limit"] == 1
    assert preview["census_attempt_limit"] == 1
    assert preview["pass_timeout_seconds"] == 12.0
    assert preview["evaluation_timeout_seconds"] == 12.0
    assert preview["meta_calls"] == 0
    assert preview["outreach"] == "disabled"
    assert len(preview["units"]) == 3
    captured: dict[str, Any] = {}

    def _capture(connect_fn: Any, config: Any) -> dict[str, Any]:
        captured["config"] = config
        return {"outcome": "completed"}

    monkeypatch.setattr("app.discovery_runner.run_worker", _capture)
    code = main([
        "run", "--scope", "pilot-dfw", "--envelope", _dallas_envelope_path(),
    ])
    assert code == 0
    executed = captured["config"]
    assert executed.max_passes == preview["max_passes"] == 3
    assert executed.max_created_total == 6
    assert executed.max_created_per_unit == 2
    assert executed.max_discovery_attempts_per_unit == 1
    assert executed.evaluation_attempt_limit == 1
    assert executed.census_attempt_limit == 1
    assert executed.max_overpass_requests == 3
    assert executed.max_census_requests == 6
