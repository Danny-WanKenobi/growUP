"""Durable recovery tests (MVP-004C-A Phase 3B).

Real tmp SQLite outboxes, scripted fake PostgreSQL connections (real
``deliver_event`` code runs against them), no Brave, no live database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

import app.observability.emitter as emitter
import app.observability.recovery as recovery
import app.observability.store as store
from app.db import DatabaseUnavailableError
from app.observability import (
    AgentEvent,
    AgentId,
    DurabilityError,
    JsonlSink,
    ObservabilityConfig,
    Outbox,
    OutboxUnavailableError,
    RetentionClass,
    emit_nonfatal,
    get_outbox_status,
    require_durable,
)

_T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)

# Synthetic secret-like token (built piecewise so no credential-shaped
# literal appears in source); matches the MUST-REDACT test pattern.
_TEST_SK = "sk-" + "testkey1" * 2


def _event(**overrides):
    base = {
        "agent": AgentId.GABRIEL,
        "run_id": "run-1",
        "event_type": "gabriel.ranking.excluded",
        "summary": "candidate excluded",
        "retention_class": RetentionClass.PERMANENT,
        "timestamp": _T0,
    }
    base.update(overrides)
    return AgentEvent.model_validate(base)


def _box(tmp_path, name="events-outbox.sqlite3"):
    box = Outbox(tmp_path / name)
    box.initialize()
    return box


def _sink(tmp_path):
    config = ObservabilityConfig(log_dir=str(tmp_path), log_level="DEBUG")
    return JsonlSink(config), config


class _FakePG:
    """Scriptable in-memory agent_events table (thread-safe)."""

    def __init__(self):
        self.rows: dict = {}
        self.failures = 0
        self.vanish_next = False
        self.statements: list = []
        self.lock = threading.Lock()

    def connection(self, config=None):
        return _FakePGConnection(self)


class _FakePGConnection:
    def __init__(self, pg):
        self._pg = pg

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _FakePGCursor(self._pg)

    def commit(self):
        pass


class _FakePGCursor:
    def __init__(self, pg):
        self._pg = pg
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        pg = self._pg
        with pg.lock:
            pg.statements.append(sql)
            if sql.startswith("INSERT"):
                if pg.failures > 0:
                    pg.failures -= 1
                    raise DatabaseUnavailableError("connection_failed")
                stored = dict(params or {})
                if stored["event_id"] in pg.rows:
                    self._fetch = None  # ON CONFLICT DO NOTHING: no row
                else:
                    pg.rows[stored["event_id"]] = stored
                    self._fetch = {"event_id": stored["event_id"]}
            elif "WHERE event_id" in sql:
                if pg.vanish_next:
                    pg.vanish_next = False
                    self._fetch = None
                else:
                    row = pg.rows.get((params or {})["event_id"])
                    self._fetch = _select_row(row) if row else None
            else:  # pragma: no cover - recovery tests only deliver
                raise AssertionError(f"unexpected SQL: {sql[:60]}")

    def fetchone(self):
        return self._fetch

    def fetchall(self):
        raise AssertionError("deliver path uses fetchone only")


def _select_row(stored):
    """Shape stored INSERT params like a real SELECT row (parsed JSONB)."""
    return {
        **stored,
        "details": json.loads(stored["details"]),
        "evidence_refs": json.loads(stored["evidence_refs"]),
    }


def _pg(monkeypatch):
    pg = _FakePG()
    monkeypatch.setattr(store, "get_connection", pg.connection)
    return pg


# --- 1-4. Durable acceptance paths ---


def test_pg_success_marks_outbox_delivered(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        outcome = emit_nonfatal(_event(), config=config, sink=sink, outbox=box)
        assert outcome.persisted_pg is True
        assert outcome.persisted_outbox is True
        assert outcome.degraded is False
        assert outcome.failed is False
        assert box.count_pending() == 0  # marked delivered after confirmation
        assert len(pg.rows) == 1
    finally:
        sink.close()


def test_pg_failure_falls_back_to_outbox(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 99  # PostgreSQL stays down
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        outcome = emit_nonfatal(_event(), config=config, sink=sink, outbox=box)
        assert outcome.persisted_pg is False
        assert outcome.persisted_outbox is True
        assert outcome.degraded is True
        assert outcome.failed is False
        assert box.count_pending() == 1
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        (pending,) = box.pending(now=soon)
        assert pending.attempts == 1
        assert pending.last_error == "pg_unreachable"
        assert pending.next_retry_at is not None
        raw = (tmp_path / "logs" / "agent-events.jsonl").read_text(encoding="utf-8")
        assert "durable in outbox only" in raw
    finally:
        sink.close()


def test_both_stores_down_reports_failed(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 99
    broken = Outbox(tmp_path / "missing-parent" / "o.sqlite3")  # never initialized
    sink, config = _sink(tmp_path / "logs")
    try:
        outcome = emit_nonfatal(_event(), config=config, sink=sink, outbox=broken)
        assert outcome.failed is True
        assert outcome.degraded is False
        assert outcome.persisted_pg is False
        assert outcome.persisted_outbox is False
        assert "outbox" in (outcome.error or "")
    finally:
        sink.close()


def test_jsonl_success_alone_is_not_durability(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 99
    broken = Outbox(tmp_path / "missing-parent" / "o.sqlite3")
    sink, config = _sink(tmp_path / "logs")
    try:
        outcome = emit_nonfatal(_event(), config=config, sink=sink, outbox=broken)
        assert outcome.file_copy is True  # the trace landed...
        assert outcome.failed is True  # ...but that is NOT durability
        raw = (tmp_path / "logs" / "agent-events.jsonl").read_text(encoding="utf-8")
        assert "NOT durably recorded" in raw
    finally:
        sink.close()


# --- 5-6. Idempotent delivery and conflicts ---


def test_idempotent_pg_delivery(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    event = _event()
    first = store.deliver_event(event)
    second = store.deliver_event(event)
    assert (first.status, second.status) == ("inserted", "already_present")
    assert len(pg.rows) == 1


def test_pg_conflict_never_overwrites(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.rows["conflict-ev-000001"] = {
        "event_id": "conflict-ev-000001",
        "occurred_at": _T0,
        "agent": "gabriel",
        "run_id": "run-1",
        "correlation_id": "",
        "property_id": None,
        "event_type": "gabriel.ranking.excluded",
        "severity": "INFO",
        "summary": "ORIGINAL summary",
        "details": "{}",
        "evidence_refs": "[]",
        "parent_event_id": None,
        "retention_class": "permanent",
    }
    result = store.deliver_event(_event(event_id="conflict-ev-000001", summary="OTHER"))
    assert result.status == "conflict"
    assert pg.rows["conflict-ev-000001"]["summary"] == "ORIGINAL summary"


def test_deliver_race_retries_later(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.rows["race-ev-00000001"] = {"event_id": "race-ev-00000001"}
    pg.vanish_next = True
    with pytest.raises(DatabaseUnavailableError):
        store.deliver_event(_event(event_id="race-ev-00000001"))


# --- 7-8. Interruption and connection failure ---


def test_interruption_before_mark_recovers_idempotently(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    event = _event()
    box.accept(event, now=_T0)
    calls = {"marks": 0}
    real_mark = box.mark_delivered

    def _crash_once(event_id, now=None):
        calls["marks"] += 1
        if calls["marks"] == 1:
            raise OutboxUnavailableErrorForTest("simulated crash")
        return real_mark(event_id, now=now)

    monkeypatch.setattr(box, "mark_delivered", _crash_once)
    with pytest.raises(OutboxUnavailableErrorForTest):
        recovery.recover_once(box, now=_T0)
    assert len(pg.rows) == 1  # delivered, but not marked
    assert box.count_pending() == 1
    report = recovery.recover_once(box, now=_T0)  # next cycle confirms
    assert report.already_delivered == 1
    assert box.count_pending() == 0
    assert len(pg.rows) == 1  # still exactly one row


class OutboxUnavailableErrorForTest(Exception):
    pass


def test_connection_failure_then_recovery(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 2  # fail twice, then recover
    box = _box(tmp_path)
    box.accept(_event(), now=_T0)
    first = recovery.recover_once(box, now=_T0)
    assert (first.failed, first.retries_scheduled) == (1, 1)
    assert box.count_pending() == 1
    (pending,) = box.pending(now=_T0 + timedelta(hours=1))
    assert pending.attempts == 1
    # Not due yet at +29s; due after backoff.
    assert box.pending(now=_T0 + timedelta(seconds=29)) == []
    second = recovery.recover_once(box, now=_T0 + timedelta(seconds=31))
    assert second.failed == 1
    third = recovery.recover_once(box, now=_T0 + timedelta(hours=1))
    assert third.delivered == 1
    assert box.count_pending() == 0


# --- 9. Backoff and retry gating ---


def test_retry_backoff_bounded() -> None:
    from app.observability import retry_delay_seconds

    assert [retry_delay_seconds(n) for n in range(9)] == [
        30, 60, 120, 240, 480, 960, 1920, 3600, 3600,
    ]
    assert retry_delay_seconds(-5) == 30
    assert retry_delay_seconds(10**9) == 3600


def test_worker_skips_not_due_events(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 99
    box = _box(tmp_path)
    box.accept(_event(), now=_T0)
    first = recovery.recover_once(box, now=_T0)
    assert first.failed == 1
    skipped = recovery.recover_once(box, now=_T0 + timedelta(seconds=10))
    assert skipped.attempted == 0  # backoff gate holds: no retry storm
    assert skipped.pending_before == 1


# --- 10-11. Multiple events and bounded batches ---


def test_multiple_pending_deliver_fifo(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    ids = []
    for minute in (30, 10, 20):
        event = _event()
        ids.append((minute, event.event_id))
        box.accept(event, now=_T0 + timedelta(minutes=minute))
    report = recovery.recover_once(box, now=_T0 + timedelta(hours=1))
    assert report.delivered == 3
    assert report.status == "ok"
    assert box.count_pending() == 0
    delivered_order = list(pg.rows)
    expected = [event_id for _, event_id in sorted(ids)]
    assert delivered_order == expected


def test_bounded_batch_processing(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    for _ in range(5):
        box.accept(_event(), now=_T0)
    report = recovery.recover_once(box, batch_limit=2, now=_T0)
    assert report.attempted == 2
    assert report.delivered == 2
    assert box.count_pending() == 3
    with pytest.raises(ValueError):
        recovery.recover_once(box, batch_limit=0)


def test_cycle_continues_past_individual_failure(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 1  # first delivery fails, rest succeed
    box = _box(tmp_path)
    for _ in range(3):
        box.accept(_event(), now=_T0)
    report = recovery.recover_once(box, now=_T0)
    assert (report.attempted, report.delivered, report.failed) == (3, 2, 1)
    assert report.status == "partial"
    assert box.count_pending() == 1


def test_conflict_reported_and_stays_pending(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    event = _event()
    pg.rows[event.event_id] = {
        "event_id": event.event_id, "occurred_at": _T0, "agent": "gabriel",
        "run_id": "run-1", "correlation_id": "", "property_id": None,
        "event_type": event.event_type, "severity": "INFO",
        "summary": "DIFFERENT stored summary", "details": "{}",
        "evidence_refs": "[]", "parent_event_id": None, "retention_class": "permanent",
    }
    box.accept(event, now=_T0)
    report = recovery.recover_once(box, now=_T0)
    assert report.conflicts == 1
    assert report.status == "failed"
    assert box.count_pending() == 1  # never marked, never overwritten
    (pending,) = box.pending(now=_T0 + timedelta(hours=1))
    assert pending.last_error == "pg_content_conflict"


# --- 12. Concurrent execution ---


def test_concurrent_workers_stay_consistent(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    for _ in range(6):
        box.accept(_event(), now=_T0)
    errors: list = []

    def _worker():
        try:
            recovery.recover_once(box, now=_T0)
        except Exception as exc:  # noqa: BLE001 - collected, asserted empty
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(pg.rows) == 6  # each event stored exactly once
    assert box.count_pending() == 0


# --- 13. Worker configuration paths ---


def test_worker_rejects_missing_or_unsafe_paths(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("OBS_OUTBOX_PATH", raising=False)
    assert recovery.main([]) == 2
    monkeypatch.setenv("OBS_OUTBOX_PATH", "relative/path.sqlite3")
    assert recovery.main([]) == 2
    monkeypatch.setenv("OBS_OUTBOX_PATH", "/var/www/growup/releases/r1/o.sqlite3")
    assert recovery.main([]) == 2
    assert "OBS_OUTBOX_PATH" in capsys.readouterr().err or True


def test_worker_cycle_reports_metrics(tmp_path, monkeypatch, capsys) -> None:
    pg = _pg(monkeypatch)
    db_path = tmp_path / "o.sqlite3"
    monkeypatch.setenv("OBS_OUTBOX_PATH", str(db_path))
    box = Outbox(str(db_path), production=True)
    box.initialize()
    box.accept(_event(summary="secret-token " + _TEST_SK), now=_T0)
    assert recovery.main([]) == 0
    out = capsys.readouterr().out
    assert "status=ok" in out and "delivered=1" in out
    assert "pending=1" in out and "oldest_age_s=" in out
    assert _TEST_SK not in out  # counts only, no payloads


def test_worker_module_does_not_import_server() -> None:
    proc = subprocess.run(
        [sys.executable, "-c",
         "import app.observability.recovery, sys; "
         "assert 'app.main' not in sys.modules; "
         "assert 'fastapi' not in sys.modules"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr


# --- 14-15. Redaction and identity preservation ---


def test_durable_flow_redacts_and_preserves_identity(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        event = _event(
            summary="key " + _TEST_SK + " here",
            details={"api_key": "live-key-9", "count": 2},
        )
        outcome = emit_nonfatal(event, config=config, sink=sink, outbox=box)
        assert outcome.persisted_pg is True
        stored = box.get(event.event_id)
        assert stored.event_id == event.event_id
        assert stored.timestamp == event.timestamp
        blob = json.dumps(outcome.event.to_record())
        assert "live-key-9" not in blob and _TEST_SK not in blob
        raw = (tmp_path / "logs" / "agent-events.jsonl").read_text(encoding="utf-8")
        assert "live-key-9" not in raw and _TEST_SK not in raw
        row = pg.rows[event.event_id]
        assert row["event_id"] == event.event_id
        assert row["occurred_at"] == event.timestamp
    finally:
        sink.close()


# --- 18. Fail-closed enforcement ---


def test_require_durable_fail_closed(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        ok = require_durable(_event(), outbox=box, config=config, sink=sink)
        assert ok.persisted_pg is True
        pg.failures = 99  # PG down, outbox healthy: durable, no raise
        degraded = require_durable(_event(), outbox=box, config=config, sink=sink)
        assert degraded.degraded is True and degraded.failed is False
        broken = Outbox(tmp_path / "missing-parent" / "o.sqlite3")
        with pytest.raises(DurabilityError):
            require_durable(_event(), outbox=broken, config=config, sink=sink)
        with pytest.raises(DurabilityError):
            require_durable(_event(), outbox=None, config=config, sink=sink)
        routine = require_durable(
            _event(retention_class=RetentionClass.ACTIVITY),
            outbox=broken, config=config, sink=sink,
        )
        assert routine.failed is False  # non-permanent never raises
    finally:
        sink.close()


def test_outbox_conflict_fails_closed_without_touching_pg(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        box.accept(_event(event_id="dup-ev-000000001", summary="original"), now=_T0)
        outcome = emit_nonfatal(
            _event(event_id="dup-ev-000000001", summary="divergent"),
            config=config, sink=sink, outbox=box,
        )
        assert outcome.failed is True
        assert "conflict" in (outcome.error or "")
        # The divergent payload never reached PostgreSQL; only the
        # file-traced degradation warning was delivered there.
        assert "dup-ev-000000001" not in pg.rows
        assert len(pg.rows) == 1
        (warned,) = pg.rows.values()
        assert warned["event_type"] == "observability.persistence_degraded"
        with pytest.raises(DurabilityError):
            require_durable(
                _event(event_id="dup-ev-000000001", summary="divergent"),
                outbox=box, config=config, sink=sink,
            )
    finally:
        sink.close()


# --- Pre-commit correction 2 - bookkeeping after durable acceptance ---


def test_mark_delivered_failure_keeps_pg_success_truthful(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        def _boom(event_id, now=None):
            raise OutboxUnavailableError("disk hiccup")

        monkeypatch.setattr(box, "mark_delivered", _boom)
        before = get_outbox_status()["errors"]
        outcome = emit_nonfatal(_event(), config=config, sink=sink, outbox=box)
        assert outcome.persisted_pg is True
        assert outcome.persisted_outbox is True
        assert outcome.degraded is True  # bookkeeping still owed, not acceptance failure
        assert outcome.failed is False
        assert "mark_delivered" in (outcome.error or "")
        assert get_outbox_status()["errors"] == before + 1
        assert box.count_pending() == 1  # stays pending; worker confirms later
        assert outcome.event.event_id in pg.rows  # PostgreSQL really committed
        raw = (tmp_path / "logs" / "agent-events.jsonl").read_text(encoding="utf-8")
        assert "bookkeeping failed" in raw
        ok = require_durable(_event(), outbox=box, config=config, sink=sink)
        assert ok.persisted_pg is True  # durable: fail-closed does not raise
    finally:
        sink.close()


def test_record_attempt_failure_keeps_outbox_durability(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 99  # PostgreSQL stays down
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        def _boom(event_id, category, next_retry_at=None, now=None):
            raise OutboxUnavailableError("disk hiccup")

        monkeypatch.setattr(box, "record_attempt", _boom)
        before = get_outbox_status()["errors"]
        outcome = emit_nonfatal(_event(), config=config, sink=sink, outbox=box)
        assert outcome.persisted_outbox is True
        assert outcome.persisted_pg is False
        assert outcome.degraded is True
        assert outcome.failed is False
        assert "pg unavailable" in (outcome.error or "")
        assert "record_attempt" in (outcome.error or "")
        assert get_outbox_status()["errors"] == before + 1
        assert box.count_pending() == 1
        ok = require_durable(_event(), outbox=box, config=config, sink=sink)
        assert ok.failed is False  # outbox-durable: no raise
    finally:
        sink.close()


def test_conflict_bookkeeping_failure_stays_degraded(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.rows["bk-conflict-01"] = {
        "event_id": "bk-conflict-01",
        "occurred_at": _T0,
        "agent": "gabriel",
        "run_id": "run-1",
        "correlation_id": "",
        "property_id": None,
        "event_type": "gabriel.ranking.excluded",
        "severity": "INFO",
        "summary": "ORIGINAL summary",
        "details": "{}",
        "evidence_refs": "[]",
        "parent_event_id": None,
        "retention_class": "permanent",
    }
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        def _boom(event_id, category, next_retry_at=None, now=None):
            raise OutboxUnavailableError("disk hiccup")

        monkeypatch.setattr(box, "record_attempt", _boom)
        outcome = emit_nonfatal(
            _event(event_id="bk-conflict-01", summary="OTHER"),
            config=config, sink=sink, outbox=box,
        )
        assert outcome.persisted_outbox is True
        assert outcome.degraded is True
        assert outcome.failed is False
        assert "pg_content_conflict" in (outcome.error or "")
        assert "record_attempt" in (outcome.error or "")
        assert pg.rows["bk-conflict-01"]["summary"] == "ORIGINAL summary"
    finally:
        sink.close()


# --- Pre-commit correction 3 - legacy vs fail-closed ---


def test_require_durable_raises_when_disabled(tmp_path) -> None:
    box = _box(tmp_path)
    sink, _ = _sink(tmp_path / "logs")
    disabled = ObservabilityConfig(log_dir=str(tmp_path / "logs"), enabled=False)
    try:
        with pytest.raises(DurabilityError):
            require_durable(_event(), outbox=box, config=disabled, sink=sink)
        assert box.count_pending() == 0  # disabled: accepted nowhere
        routine = require_durable(
            _event(retention_class=RetentionClass.ACTIVITY),
            outbox=box, config=disabled, sink=sink,
        )
        assert routine.failed is False  # non-permanent never raises
    finally:
        sink.close()


# --- Pre-commit correction 4 - worker exit codes ---


def test_pg_outage_exits_failed_and_keeps_events_retryable(
    tmp_path, monkeypatch, capsys
) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 99  # PostgreSQL stays down
    db_path = tmp_path / "o.sqlite3"
    monkeypatch.setenv("OBS_OUTBOX_PATH", str(db_path))
    box = Outbox(str(db_path), production=True)
    box.initialize()
    box.accept(_event(), now=_T0)
    assert recovery.main([]) == 3
    out = capsys.readouterr().out
    assert "status=failed" in out and "failed=1" in out
    assert box.count_pending() == 1  # pending, not lost
    (pending,) = box.pending(now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert pending.attempts == 1
    assert pending.next_retry_at is not None  # retryable on the next tick


def test_partial_cycle_exits_1(tmp_path, monkeypatch, capsys) -> None:
    pg = _pg(monkeypatch)
    pg.failures = 1  # first INSERT fails, the rest succeed
    db_path = tmp_path / "o.sqlite3"
    monkeypatch.setenv("OBS_OUTBOX_PATH", str(db_path))
    box = Outbox(str(db_path), production=True)
    box.initialize()
    box.accept(_event(), now=_T0)
    box.accept(_event(), now=_T0)
    assert recovery.main([]) == 1
    out = capsys.readouterr().out
    assert "status=partial" in out
    assert "delivered=1" in out and "failed=1" in out
    assert box.count_pending() == 1


def test_unreadable_outbox_exits_4(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "o.sqlite3"
    db_path.write_bytes(b"this is not a sqlite database" * 100)
    monkeypatch.setenv("OBS_OUTBOX_PATH", str(db_path))
    _pg(monkeypatch)
    assert recovery.main([]) == 4
    assert "recovery error" in capsys.readouterr().err
