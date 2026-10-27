"""Operation classification and durability enforcement (Phase 3C-1).

Real tmp SQLite outboxes, minimal fake PostgreSQL (INSERT-only; each
test uses fresh event IDs), no Brave, no live database, no business
mutations. Verifies the policy contract only -- no workflow in this
repo calls audit_event yet.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import app.observability.policy as policy
import app.observability.store as store
from app.db import DatabaseUnavailableError
from app.observability import (
    AgentEvent,
    AgentId,
    DurabilityError,
    JsonlSink,
    ObservabilityConfig,
    OperationClass,
    Outbox,
    RetentionClass,
    audit_event,
    policy_for,
)

_T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


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


def _box(tmp_path, name="events-outbox.sqlite3"):
    box = Outbox(tmp_path / name)
    box.initialize()
    return box


def _broken_box(tmp_path):
    return Outbox(tmp_path / "missing-parent" / "o.sqlite3")  # never initialized


def _sink(tmp_path):
    config = ObservabilityConfig(log_dir=str(tmp_path), log_level="DEBUG")
    return JsonlSink(config), config


class _PG:
    """Minimal fake: INSERTs succeed unless the service is down."""

    def __init__(self):
        self.down = False
        self.rows: dict = {}

    def connection(self, config=None):
        return _Connection(self)


class _Connection:
    def __init__(self, pg):
        self._pg = pg

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _Cursor(self._pg)

    def commit(self):
        pass


class _Cursor:
    def __init__(self, pg):
        self._pg = pg
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        if self._pg.down:
            raise DatabaseUnavailableError("connection_failed")
        if not sql.startswith("INSERT"):
            raise AssertionError(f"unexpected SQL: {sql[:60]}")
        stored = dict(params or {})
        self._pg.rows[stored["event_id"]] = stored
        self._fetch = {"event_id": stored["event_id"]}

    def fetchone(self):
        return self._fetch


def _pg(monkeypatch):
    pg = _PG()
    monkeypatch.setattr(store, "get_connection", pg.connection)
    return pg


# --- 1. Classification policies ---


def test_every_class_has_expected_policy() -> None:
    assert set(OperationClass) == {
        OperationClass.PRELIMINARY,
        OperationClass.CONSEQUENTIAL_INTERNAL,
        OperationClass.CONSEQUENTIAL_EXTERNAL,
    }
    assert policy_for(OperationClass.PRELIMINARY).requires_durable_audit is False
    assert policy_for(OperationClass.CONSEQUENTIAL_INTERNAL).requires_durable_audit is True
    assert policy_for(OperationClass.CONSEQUENTIAL_EXTERNAL).requires_durable_audit is True
    for cls in OperationClass:
        assert policy_for(cls).operation_class is cls
        assert policy_for(cls).summary.strip()


def test_classification_rejects_non_members() -> None:
    for bogus in ("preliminary", "CONSEQUENTIAL_INTERNAL", None, 42, object()):
        with pytest.raises(TypeError):
            policy_for(bogus)
        with pytest.raises(TypeError):
            audit_event(_event(), bogus)


# --- 2. Preliminary stays nonfatal ---


def test_preliminary_healthy_path_emits_normally(tmp_path, monkeypatch) -> None:
    _pg(monkeypatch)
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        outcome = audit_event(
            _event(), OperationClass.PRELIMINARY, outbox=box, config=config, sink=sink
        )
        assert outcome.persisted_pg is True
        assert outcome.failed is False
    finally:
        sink.close()


def test_preliminary_total_outage_is_nonfatal(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.down = True
    broken = _broken_box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        outcome = audit_event(
            _event(), OperationClass.PRELIMINARY, outbox=broken, config=config, sink=sink
        )
        assert outcome.failed is True  # nowhere durable, yet no raise
        assert outcome.file_copy is True
    finally:
        sink.close()


# --- 3-7. Consequential requires durable acceptance ---


def test_consequential_pg_acceptance_satisfies(tmp_path, monkeypatch) -> None:
    _pg(monkeypatch)
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        for cls in (
            OperationClass.CONSEQUENTIAL_INTERNAL,
            OperationClass.CONSEQUENTIAL_EXTERNAL,
        ):
            outcome = audit_event(_event(), cls, outbox=box, config=config, sink=sink)
            assert outcome.persisted_pg is True
            assert outcome.failed is False
    finally:
        sink.close()


def test_consequential_outbox_acceptance_satisfies_when_pg_down(
    tmp_path, monkeypatch
) -> None:
    pg = _pg(monkeypatch)
    pg.down = True
    box = _box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        for cls in (
            OperationClass.CONSEQUENTIAL_INTERNAL,
            OperationClass.CONSEQUENTIAL_EXTERNAL,
        ):
            outcome = audit_event(_event(), cls, outbox=box, config=config, sink=sink)
            assert outcome.persisted_outbox is True
            assert outcome.degraded is True
            assert outcome.failed is False
        assert box.count_pending() == 2  # recovery timer forwards both
    finally:
        sink.close()


def test_jsonl_only_does_not_satisfy_consequential(tmp_path, monkeypatch) -> None:
    pg = _pg(monkeypatch)
    pg.down = True
    broken = _broken_box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        with pytest.raises(DurabilityError):
            audit_event(
                _event(),
                OperationClass.CONSEQUENTIAL_EXTERNAL,
                outbox=broken,
                config=config,
                sink=sink,
            )
        raw = (tmp_path / "logs" / "agent-events.jsonl").read_text(encoding="utf-8")
        assert "test.decision" in raw  # JSONL trace worked, yet still raised
    finally:
        sink.close()


def test_both_stores_down_raises_for_both_consequential_classes(
    tmp_path, monkeypatch
) -> None:
    pg = _pg(monkeypatch)
    pg.down = True
    broken = _broken_box(tmp_path)
    sink, config = _sink(tmp_path / "logs")
    try:
        for cls in (
            OperationClass.CONSEQUENTIAL_INTERNAL,
            OperationClass.CONSEQUENTIAL_EXTERNAL,
        ):
            with pytest.raises(DurabilityError):
                audit_event(_event(), cls, outbox=broken, config=config, sink=sink)
    finally:
        sink.close()


# --- 8. The interface only requests audit acceptance ---


def test_audit_event_delegates_without_business_logic(tmp_path, monkeypatch) -> None:
    calls: list = []

    def _fake_require(event, outbox=None, config=None, sink=None):
        calls.append(("require_durable", event))
        return "durable-outcome"

    def _fake_nonfatal(event, config=None, sink=None, outbox=None):
        calls.append(("emit_nonfatal", event))
        return "nonfatal-outcome"

    monkeypatch.setattr(policy, "require_durable", _fake_require)
    monkeypatch.setattr(policy, "emit_nonfatal", _fake_nonfatal)
    event = _event()
    assert audit_event(event, OperationClass.CONSEQUENTIAL_INTERNAL) == "durable-outcome"
    assert audit_event(event, OperationClass.CONSEQUENTIAL_EXTERNAL) == "durable-outcome"
    assert audit_event(event, OperationClass.PRELIMINARY) == "nonfatal-outcome"
    assert [kind for kind, _ in calls] == [
        "require_durable",
        "require_durable",
        "emit_nonfatal",
    ]
    assert all(seen is event for _, seen in calls)  # same event, no mutation


def test_policy_module_imports_no_business_code() -> None:
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/observability/policy.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    app_imports = {m for m in imported if m == "app" or m.startswith("app.")}
    assert app_imports <= {
        "app.observability.config",
        "app.observability.emitter",
        "app.observability.events",
        "app.observability.jsonlog",
        "app.observability.outbox",
    }, app_imports


# --- 9. No Gabriel workflow is fail-closed ---


def test_gabriel_workflows_do_not_use_policy() -> None:
    import ast
    from pathlib import Path

    fail_closed_names = {"require_durable", "audit_event", "OperationClass", "policy_for"}
    offenders: dict = {}
    for source in sorted(Path("app/agents/gabriel").glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        hit = fail_closed_names & names
        if hit:
            offenders[source.name] = sorted(hit)
    assert offenders == {}, offenders
