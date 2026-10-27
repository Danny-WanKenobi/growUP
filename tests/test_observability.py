"""Shared observability tests (MVP-004C-A): no network, no live database.

PostgreSQL interactions use fake connections asserting exact SQL shapes
and parameters; file-sink tests use tmp_path. Live-database verification
remains a documented deployment prerequisite.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

import app.observability.emitter as emitter
import app.observability.store as store
from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError
from app.observability import (
    AgentEvent,
    AgentId,
    EvidenceRef,
    EventPersistenceError,
    JsonlSink,
    ObservabilityConfig,
    RetentionClass,
    Severity,
    by_correlation,
    log_activity,
    log_decision,
    log_diagnostic,
    log_error,
    log_evidence,
    log_handoff,
    log_warning,
    purge_expired_events,
    purge_old_log_files,
    query_events,
    redact,
)


def _event(**overrides):
    base = {
        "agent": AgentId.GABRIEL,
        "run_id": "run-1",
        "event_type": "test.probe",
        "summary": "probe event",
    }
    base.update(overrides)
    return AgentEvent.model_validate(base)


def _sink(tmp_path, **overrides):
    params = {"log_level": "DEBUG"}
    params.update(overrides)
    config = ObservabilityConfig(log_dir=str(tmp_path), **params)
    return JsonlSink(config), config


def _lines(tmp_path):
    path = tmp_path / "agent-events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class _FakeCursor:
    def __init__(self, recorder, rows=None, rowcount=0):
        self._recorder = recorder
        self._rows = rows
        self.rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self._recorder.setdefault("statements", []).append((sql, params))

    def fetchall(self):
        return self._rows or []


class _FakeConnection:
    def __init__(self, recorder, rows=None, rowcount=0):
        self._recorder = recorder
        self._rows = rows
        self._rowcount = rowcount

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _FakeCursor(self._recorder, self._rows, self._rowcount)

    def commit(self):
        self._recorder["committed"] = True


def _row(**overrides):
    base = {
        "event_id": "event-0001",
        "occurred_at": datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
        "agent": "gabriel",
        "run_id": "run-1",
        "correlation_id": "",
        "property_id": None,
        "event_type": "test.probe",
        "severity": "INFO",
        "summary": "probe",
        "details": {},
        "evidence_refs": [],
        "parent_event_id": None,
        "retention_class": "activity",
    }
    base.update(overrides)
    return base


def _no_pg(monkeypatch) -> None:
    """Detach PG writes for file-focused tests (no network, no noise)."""
    monkeypatch.setattr(emitter, "insert_event", lambda event, config=None: event)


# --- 1. Event schema validation ---


def test_event_schema_accepts_valid_and_rejects_invalid() -> None:
    event = _event(
        correlation_id="corr-1",
        property_id=uuid4(),
        severity=Severity.WARNING,
        details={"count": 3},
        evidence_refs=[{"kind": "listing_url", "ref": "https://example.com/x"}],
        parent_event_id="parent-1",
        retention_class=RetentionClass.PERMANENT,
    )
    assert event.agent is AgentId.GABRIEL
    with pytest.raises(ValidationError):
        _event(event_type="Bad Name!")
    with pytest.raises(ValidationError):
        _event(event_type="x")
    with pytest.raises(ValidationError):
        _event(summary="")
    with pytest.raises(ValidationError):
        _event(agent="unknown-agent")
    with pytest.raises(ValidationError):
        _event(severity="CRITICAL")
    with pytest.raises(ValidationError):
        _event(run_id="")
    circular: dict = {}
    circular["self"] = circular
    with pytest.raises(ValidationError):
        _event(details=circular)
    with pytest.raises(ValidationError):
        _event(details={"blob": "x" * 70000})
    with pytest.raises(ValidationError):
        _event(evidence_refs=[{"kind": "k", "ref": "r"}] * 201)


# --- 2. Unique event identifiers ---


def test_event_ids_unique() -> None:
    ids = {_event().event_id for _ in range(500)}
    assert len(ids) == 500
    assert all(len(i) == 32 and all(c in "0123456789abcdef" for c in i) for i in ids)


# --- 3. UTC timestamps ---


def test_timestamps_utc_and_coerced() -> None:
    event = _event()
    assert event.timestamp.tzinfo is not None
    assert event.timestamp.utcoffset().total_seconds() == 0
    naive = _event(timestamp=datetime(2026, 1, 1, 12, 0, 0))
    assert naive.timestamp.tzinfo is not None
    assert naive.timestamp.isoformat() == "2026-01-01T12:00:00+00:00"
    parsed = _event(timestamp="2026-10-27T12:00:00Z")
    assert parsed.timestamp.utcoffset().total_seconds() == 0
    assert event.to_record()["timestamp"].endswith("+00:00")


# --- 4. Retention classes and routing ---


def test_routing_by_retention_class(monkeypatch, tmp_path) -> None:
    sink, config = _sink(tmp_path)
    try:
        pg_calls: list = []
        monkeypatch.setattr(
            emitter, "insert_event", lambda event, config=None: pg_calls.append(event)
        )
        log_diagnostic("gabriel", "r1", "test.debug", "d", config=config, sink=sink)
        assert pg_calls == []  # diagnostic: file only
        log_activity("gabriel", "r1", "test.activity", "a", config=config, sink=sink)
        log_decision("gabriel", "r1", "test.decision", "d", config=config, sink=sink)
        log_evidence("gabriel", "r1", "test.evidence", "e", config=config, sink=sink)
        log_handoff("gabriel", "r1", "raphael", "h", config=config, sink=sink)
        assert len(pg_calls) == 4
        assert [e.retention_class for e in pg_calls] == [
            RetentionClass.ACTIVITY,
            RetentionClass.PERMANENT,
            RetentionClass.PERMANENT,
            RetentionClass.PERMANENT,
        ]
        assert len(_lines(tmp_path)) == 5  # every event also traced to file
    finally:
        sink.close()


# --- 5. JSON serialization ---


def test_json_serialization_roundtrip(tmp_path, monkeypatch) -> None:
    _no_pg(monkeypatch)
    sink, config = _sink(tmp_path)
    try:
        event = log_activity(
            "wondrue", "r9", "test.roundtrip", "roundtrip",
            details={"nested": [1, "two", {"three": 3}]},
            evidence_refs=[{"kind": "url", "ref": "https://example.com"}],
            config=config, sink=sink,
        )
        (record,) = _lines(tmp_path)
        assert record == event.to_record()
        assert json.loads(json.dumps(record)) == record
    finally:
        sink.close()


# --- 6. Redaction of secrets ---


def test_redaction_of_secrets(tmp_path, monkeypatch) -> None:
    _no_pg(monkeypatch)
    payload = {
        "api_key": "live-key-123",
        "nested": {"password": "hunter2", "Authorization": "Bearer abc.def.ghi"},
        "headers": {"X-Subscription-Token": "brave-secret", "Cookie": "s=1"},
        "note": "call sk-abcdefghijklmnop now",
        "safe": "visible",
        "count": 7,
    }
    scrubbed = redact(payload)
    assert scrubbed["api_key"] == "***REDACTED***"
    assert scrubbed["nested"]["password"] == "***REDACTED***"
    assert "abc.def.ghi" not in scrubbed["nested"]["Authorization"]
    assert scrubbed["headers"]["X-Subscription-Token"] == "***REDACTED***"
    assert scrubbed["headers"]["Cookie"] == "***REDACTED***"
    assert "sk-abcdefghijklmnop" not in scrubbed["note"]
    assert scrubbed["safe"] == "visible"
    assert scrubbed["count"] == 7
    sink, config = _sink(tmp_path)
    try:
        event = log_activity(
            "gabriel", "r1", "test.secrets", "token Bearer xyz.123 here",
            details=payload,
            evidence_refs=[{"kind": "note", "ref": "Bearer xyz.123"}],
            config=config, sink=sink,
        )
        raw = (tmp_path / "agent-events.jsonl").read_text(encoding="utf-8")
        for secret in ("live-key-123", "hunter2", "abc.def.ghi", "brave-secret",
                       "sk-abcdefghijklmnop", "xyz.123"):
            assert secret not in raw
            assert secret not in event.model_dump_json()
    finally:
        sink.close()


# --- 7. Metadata size limits ---


def test_metadata_size_limits(tmp_path, monkeypatch) -> None:
    _no_pg(monkeypatch)
    sink, config = _sink(tmp_path, max_detail_bytes=200, max_evidence_refs=2)
    try:
        event = log_activity(
            "gabriel", "r1", "test.bounds", "bounded",
            details={"big": "x" * 5000, "small": 1},
            evidence_refs=[{"kind": "k", "ref": f"r{i}"} for i in range(5)],
            config=config, sink=sink,
        )
        assert event.details["_truncated"] is True
        assert event.details["_original_bytes"] > 200
        assert "big" in event.details["_keys"]
        assert len(event.evidence_refs) == 2
        assert event.details["_evidence_refs_truncated"] == 3
    finally:
        sink.close()


# --- 8. Safe diagnostic log output ---


def test_safe_log_output_single_line_and_sanitized(tmp_path) -> None:
    sink, config = _sink(tmp_path)
    try:
        log_diagnostic(
            "gabriel", "r1", "test.unsafe",
            "line one\nline two\x00with null",
            details={"evil": "a\x1bb\x00c"},
            config=config, sink=sink,
        )
        raw_lines = (tmp_path / "agent-events.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(raw_lines) == 1  # newline injection cannot split records
        (record,) = [json.loads(line) for line in raw_lines]
        assert record["summary"] == "line one line twowith null"
        assert record["details"] == {"evil": "abc"}
        assert "\x00" not in json.dumps(record)
    finally:
        sink.close()


def test_log_level_filtering_and_rotation_bounds(tmp_path, monkeypatch) -> None:
    _no_pg(monkeypatch)
    sink, config = _sink(tmp_path, log_level="WARNING", log_max_bytes=300, log_backup_count=3)
    try:
        log_diagnostic("gabriel", "r1", "test.debug", "dropped", config=config, sink=sink)
        for n in range(30):
            log_warning("gabriel", "r1", "test.warn", f"warning number {n:02d} " + "x" * 60,
                        config=config, sink=sink)
        status = sink.status()
        assert status["dropped_below_level"] == 1
        files = sorted(tmp_path.iterdir())
        assert 1 < len(files) <= 4  # rotated, but bounded
        total = sum(1 for f in files for line in
                    f.read_text(encoding="utf-8").splitlines() if line.strip())
        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    json.loads(line)  # every retained line still parses
        assert total > 0
    finally:
        sink.close()


# --- 9. PostgreSQL event persistence ---


def test_pg_insert_shape_and_params(monkeypatch) -> None:
    recorder: dict = {}
    monkeypatch.setattr(
        store, "get_connection", lambda config=None: _FakeConnection(recorder)
    )
    event = _event(
        severity=Severity.ERROR,
        details={"reason": "boom"},
        evidence_refs=[{"kind": "url", "ref": "https://example.com"}],
        retention_class=RetentionClass.PERMANENT,
    )
    store.insert_event(event)
    assert recorder.get("committed") is True
    (sql, params), = recorder["statements"]
    assert sql.startswith("INSERT INTO agent_events")
    assert params["event_id"] == event.event_id
    assert params["agent"] == "gabriel"
    assert params["severity"] == "ERROR"
    assert json.loads(params["details"]) == {"reason": "boom"}
    assert json.loads(params["evidence_refs"]) == [{"kind": "url", "ref": "https://example.com"}]
    assert params["property_id"] is None
    assert params["retention_class"] == "permanent"


# --- 10. Filtering and pagination ---


def test_query_filters_pagination_and_bounds(monkeypatch) -> None:
    recorder: dict = {}
    prop = uuid4()
    monkeypatch.setattr(
        store, "get_connection",
        lambda config=None: _FakeConnection(
            recorder, rows=[_row(), _row(event_id="event-0002")]
        ),
    )
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    until = datetime(2026, 10, 31, tzinfo=timezone.utc)
    events = query_events(
        agent="gabriel", run_id="r1", correlation_id="c1", property_id=prop,
        event_type="test.probe", retention_class="activity",
        since=since, until=until, limit=5, offset=10,
    )
    assert [e.event_id for e in events] == ["event-0001", "event-0002"]
    (sql, params), = recorder["statements"]
    for fragment in ("agent = %(agent)s", "run_id = %(run_id)s",
                     "correlation_id = %(correlation_id)s", "property_id = %(property_id)s",
                     "event_type = %(event_type)s", "retention_class = %(retention_class)s",
                     "occurred_at >=", "occurred_at <=", "LIMIT %(limit)s", "OFFSET %(offset)s"):
        assert fragment in sql
    assert params["property_id"] == str(prop)
    assert (params["limit"], params["offset"]) == (5, 10)
    # Limit clamps; invalid bounds raise before any I/O.
    query_events(limit=5000)
    assert recorder["statements"][-1][1]["limit"] == 1000
    with pytest.raises(ValueError):
        query_events(limit=0)
    with pytest.raises(ValueError):
        query_events(offset=-1)


# --- 11. Deterministic ordering ---


def test_deterministic_ordering(monkeypatch) -> None:
    recorder: dict = {}
    monkeypatch.setattr(store, "get_connection", lambda config=None: _FakeConnection(recorder))
    query_events(agent="gabriel")
    assert "ORDER BY occurred_at DESC, event_id DESC" in recorder["statements"][-1][0]
    query_events(agent="gabriel", newest_first=False)
    assert "ORDER BY occurred_at ASC, event_id ASC" in recorder["statements"][-1][0]


# --- 12. Retention cleanup ---


def test_pg_retention_cleanup(monkeypatch) -> None:
    recorder: dict = {}
    monkeypatch.setattr(
        store, "get_connection", lambda config=None: _FakeConnection(recorder, rowcount=4)
    )
    counts = purge_expired_events(90, 365)
    assert counts == {"diagnostic": 4, "activity": 4}
    assert recorder.get("committed") is True
    assert len(recorder["statements"]) == 2
    (_, first), (_, second) = recorder["statements"]
    assert (first["retention"], first["days"]) == ("diagnostic", 90)
    assert (second["retention"], second["days"]) == ("activity", 365)
    with pytest.raises(ValueError):
        purge_expired_events(0, 365)


def test_file_retention_cleanup(tmp_path) -> None:
    old = time.time() - 100 * 86400
    fresh = time.time() - 10 * 86400
    aged = tmp_path / "agent-events.jsonl.2"
    aged.write_text("{}\n", encoding="utf-8")
    os.utime(aged, (old, old))
    current = tmp_path / "agent-events.jsonl"
    current.write_text("{}\n", encoding="utf-8")
    os.utime(current, (fresh, fresh))
    unrelated = tmp_path / "agent-events.jsonl.bak"
    unrelated.write_text("keep\n", encoding="utf-8")
    os.utime(unrelated, (old, old))
    deleted = purge_old_log_files(tmp_path, 90)
    assert deleted == [str(aged)]
    assert current.exists()
    assert unrelated.exists()  # non-rotated names never touched
    assert purge_old_log_files(tmp_path, 0) == []
    assert purge_old_log_files(tmp_path / "missing", 90) == []


# --- 13. Protection of permanent events ---


def test_permanent_events_unmatchable_by_cleanup(monkeypatch) -> None:
    recorder: dict = {}
    monkeypatch.setattr(store, "get_connection", lambda config=None: _FakeConnection(recorder))
    purge_expired_events(90, 365)
    for sql, params in recorder["statements"]:
        assert "retention_class != 'permanent'" in sql
        assert params["retention"] in ("diagnostic", "activity")


# --- 14. Logging failure behavior ---


def test_failure_behavior_and_no_recursion(monkeypatch, tmp_path, capsys) -> None:
    sink, config = _sink(tmp_path)
    try:
        def _down(*args, **kwargs):
            raise DatabaseUnavailableError("connection_failed")

        monkeypatch.setattr(emitter, "insert_event", _down)
        # Diagnostic and activity degrade without raising.
        log_diagnostic("gabriel", "r1", "test.d", "d", config=config, sink=sink)
        activity = log_activity("gabriel", "r1", "test.a", "a", config=config, sink=sink)
        assert activity.event_type == "test.a"
        assert len(_lines(tmp_path)) == 2  # file copies still landed
        status = emitter.get_pg_status()
        assert status["errors"] >= 1 and status["last_error"]
        # Permanent failures are never silent.
        with pytest.raises(EventPersistenceError):
            log_decision("gabriel", "r1", "test.decision", "d", config=config, sink=sink)
        assert len(_lines(tmp_path)) == 3  # best-effort file copy before raising
        # Error paths echo to stderr, never back into emit (no recursion).
        monkeypatch.setattr(emitter, "emit", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("recursive emit")))
        captured = capsys.readouterr()
        assert "observability pg error" in captured.err
    finally:
        sink.close()


def test_emit_nonfatal_permanent_failure_reports_degraded(tmp_path, monkeypatch) -> None:
    from app.observability import emit_nonfatal

    sink, config = _sink(tmp_path)
    try:
        def _down(*args, **kwargs):
            raise DatabaseUnavailableError("connection_failed")

        monkeypatch.setattr(emitter, "insert_event", _down)
        event = _event(event_type="test.decision", retention_class=RetentionClass.PERMANENT)
        outcome = emit_nonfatal(event, config=config, sink=sink)
        assert outcome.failed is True  # legacy path: no outbox, nowhere durable
        assert outcome.degraded is False
        assert outcome.persisted_pg is False
        assert outcome.persisted_outbox is False
        assert outcome.file_copy is True
        assert outcome.error
        assert outcome.event.event_id == event.event_id
        assert outcome.event.retention_class is RetentionClass.PERMANENT
        records = _lines(tmp_path)
        assert len(records) == 2
        original, fallback = records
        assert original["event_id"] == event.event_id
        assert fallback["event_type"] == "observability.persistence_degraded"
        assert fallback["parent_event_id"] == event.event_id
        assert "NOT durably recorded" in fallback["summary"]
        assert fallback["details"]["original_event_id"] == event.event_id
        assert fallback["details"]["original_event_type"] == "test.decision"
        assert fallback["details"]["file_copy"] is True
        assert "connection_failed" in fallback["details"]["detail"]
        assert fallback["details"]["persisted_outbox"] is False
    finally:
        sink.close()


def test_emit_nonfatal_permanent_success_reports_persisted(tmp_path, monkeypatch) -> None:
    from app.observability import emit_nonfatal

    sink, config = _sink(tmp_path)
    try:
        calls: list = []
        monkeypatch.setattr(
            emitter, "insert_event", lambda event, config=None: calls.append(event)
        )
        event = _event(event_type="test.decision", retention_class=RetentionClass.PERMANENT)
        outcome = emit_nonfatal(event, config=config, sink=sink)
        assert outcome.degraded is False
        assert outcome.persisted_pg is True
        assert outcome.file_copy is True
        assert outcome.error is None
        assert len(calls) == 1
        assert len(_lines(tmp_path)) == 1  # no degradation warning on success
    finally:
        sink.close()


def test_emit_nonfatal_activity_failure_is_not_degraded(tmp_path, monkeypatch) -> None:
    from app.observability import emit_nonfatal

    sink, config = _sink(tmp_path)
    try:
        def _down(*args, **kwargs):
            raise DatabaseUnavailableError("connection_failed")

        monkeypatch.setattr(emitter, "insert_event", _down)
        outcome = emit_nonfatal(_event(), config=config, sink=sink)
        assert outcome.degraded is False  # activity degradation is the contract
        assert outcome.persisted_pg is False
        assert outcome.file_copy is True
        assert len(_lines(tmp_path)) == 1  # no fallback warning for activity
    finally:
        sink.close()


def test_emit_nonfatal_disabled_reports_honestly(tmp_path) -> None:
    from app.observability import ObservabilityConfig, emit_nonfatal

    config = ObservabilityConfig(log_dir=str(tmp_path), enabled=False)
    permanent = emit_nonfatal(
        _event(event_type="test.decision", retention_class=RetentionClass.PERMANENT),
        config=config,
    )
    assert permanent.failed is True
    assert permanent.degraded is False
    assert permanent.persisted_pg is False
    assert permanent.file_copy is False
    assert permanent.error == "observability disabled"
    activity = emit_nonfatal(_event(), config=config)
    assert activity.degraded is False
    assert not (tmp_path / "agent-events.jsonl").exists()  # no sink touched


def test_unwritable_sink_degrades_without_raising(tmp_path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    sink = JsonlSink(ObservabilityConfig(log_dir=str(blocker / "logs")))
    try:
        assert sink.path is None
        assert sink.write({"severity": "INFO"}) is False
        assert sink.status()["errors"] >= 1
        # Business operations still proceed.
        event = log_diagnostic("gabriel", "r1", "test.d", "d", sink=sink)
        assert event.event_id
    finally:
        sink.close()


def test_missing_database_config_maps_cleanly(monkeypatch, tmp_path) -> None:
    sink, config = _sink(tmp_path)
    try:
        def _unconfigured(*args, **kwargs):
            raise DatabaseNotConfiguredError("database_not_configured")

        monkeypatch.setattr(emitter, "insert_event", _unconfigured)
        log_activity("gabriel", "r1", "test.a", "a", config=config, sink=sink)
        with pytest.raises(EventPersistenceError):
            log_handoff("gabriel", "r1", "raphael", "h", config=config, sink=sink)
    finally:
        sink.close()


# --- 15. Correlation IDs across agents ---


def test_correlation_across_agents(monkeypatch, tmp_path) -> None:
    sink, config = _sink(tmp_path)
    try:
        pg_calls: list = []
        monkeypatch.setattr(
            emitter, "insert_event", lambda event, config=None: pg_calls.append(event)
        )
        first = log_activity("gabriel", "run-7", "gabriel.found", "found",
                             correlation_id="corr-7", config=config, sink=sink)
        second = log_activity("raphael", "run-8", "raphael.checking", "checking",
                              correlation_id="corr-7", parent_event_id=first.event_id,
                              config=config, sink=sink)
        assert first.correlation_id == second.correlation_id == "corr-7"
        assert second.parent_event_id == first.event_id
        recorder: dict = {}
        monkeypatch.setattr(
            store, "get_connection",
            lambda config=None: _FakeConnection(
                recorder, rows=[_row(event_id=first.event_id), _row(event_id=second.event_id)]
            ),
        )
        found = by_correlation("corr-7")
        assert [e.event_id for e in found] == [first.event_id, second.event_id]
        sql, params = recorder["statements"][-1]
        assert "correlation_id = %(correlation_id)s" in sql
        assert params["correlation_id"] == "corr-7"
        assert "ORDER BY occurred_at ASC, event_id ASC" in sql
    finally:
        sink.close()


def test_wondrue_query_helpers_shape(monkeypatch) -> None:
    import app.observability as obs

    recorder: dict = {}
    monkeypatch.setattr(store, "get_connection", lambda config=None: _FakeConnection(recorder))
    prop = uuid4()
    obs.recent_by_agent("gabriel")
    obs.by_property(prop)
    obs.by_run("run-1")
    obs.decisions()
    obs.handoffs()
    assert len(recorder["statements"]) == 5
    handoff_sql, handoff_params = recorder["statements"][-1]
    assert handoff_params["event_type"] == "agent.handoff"
    assert "LIMIT %(limit)s" in handoff_sql


def test_threaded_emit_stays_parseable(tmp_path, monkeypatch) -> None:
    _no_pg(monkeypatch)
    sink, config = _sink(tmp_path)
    try:
        def _worker(n):
            for i in range(10):
                log_activity("gabriel", f"run-{n}", "test.concurrent", f"event {n}-{i}",
                             config=config, sink=sink)

        threads = [threading.Thread(target=_worker, args=(n,)) for n in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(_lines(tmp_path)) == 100
    finally:
        sink.close()


def test_warning_error_and_handoff_helpers(tmp_path, monkeypatch) -> None:
    sink, config = _sink(tmp_path)
    try:
        pg_calls: list = []
        monkeypatch.setattr(
            emitter, "insert_event", lambda event, config=None: pg_calls.append(event)
        )
        warning = log_warning("michael", "r1", "test.slow", "slow", config=config, sink=sink)
        error = log_error("uriel", "r1", "test.failed", "failed", config=config, sink=sink)
        handoff = log_handoff("gabriel", "r1", AgentId.RAPHAEL, "verify this",
                              correlation_id="c", config=config, sink=sink)
        assert (warning.severity, error.severity) == (Severity.WARNING, Severity.ERROR)
        assert handoff.event_type == "agent.handoff"
        assert handoff.details["to_agent"] == "raphael"
        assert handoff.retention_class is RetentionClass.PERMANENT
        assert len(pg_calls) == 3
    finally:
        sink.close()
