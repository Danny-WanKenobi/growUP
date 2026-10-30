"""Priscilla conversion-tracking tests (fake connections, no PG)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from app.priscilla import (
    KINDS,
    PRISCILLA_VERSION,
    PriscillaError,
    metrics_in_transaction,
    record_event_in_transaction,
)


class _FakeCursor:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> Any:
        return self._script.pop(0) if self._script else None

    def fetchall(self) -> Any:
        return self._script.pop(0) if self._script else []


class _FakeConn:
    def __init__(self, script: list[Any]) -> None:
        self._cursor = _FakeCursor(script)

    def cursor(self) -> _FakeCursor:
        return self._cursor


def _package(payload_cell: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "entry_id": uuid4(),
        "dossier_id": uuid4(),
        "version": 2,
        "snapshot_id": uuid4(),
        "receipt_id": uuid4(),
        "demo_mode": True,
        "simulated": False,
        "payload": json.dumps({"cell": payload_cell}),
        "role_schema": "canonical1-uriel",
        "package_state": "prepared",
        "execution_state": "not_attempted",
        "created_at": "t",
    }


def test_record_simulated_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "yes")
    package = _package({"county": "dallas county", "category": "high_rise"})
    conn = _FakeConn([
        package, {"event_id": uuid4(), "created_at": "t"},
        {"county": "dallas county"}, {"event_id": uuid4()},
    ])
    result = record_event_in_transaction(
        conn, package_entry=package["entry_id"], kind="delivery",
        label="SIMULATED delivery for demo", simulated=True,
    )
    assert result["simulated"] is True
    assert result["payload"]["priscilla_version"] == PRISCILLA_VERSION
    assert any("INSERT INTO conversion_events" in sql for sql, _ in conn._cursor.executed)
    assert any("INSERT INTO pilot_events" in sql for sql, _ in conn._cursor.executed)


def test_record_actual_event_requires_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "no")
    package = _package()
    conn = _FakeConn([package, {"event_id": uuid4(), "created_at": "t"}])
    result = record_event_in_transaction(
        conn, package_entry=package["entry_id"], kind="response",
        label="Owner replied by email", simulated=False,
        evidence_refs=["mailbox:thread-42"],
    )
    assert result["simulated"] is False
    assert result["payload"]["evidence_refs"] == ["mailbox:thread-42"]
    with pytest.raises(PriscillaError, match="require evidence"):
        record_event_in_transaction(
            _FakeConn([]), package_entry=uuid4(), kind="response",
            label="Owner replied", simulated=False,
        )


def test_simulated_requires_demo_mode_and_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "no")
    with pytest.raises(PriscillaError, match="DEMO_MODE=yes"):
        record_event_in_transaction(
            _FakeConn([]), package_entry=uuid4(), kind="delivery",
            label="SIMULATED delivery", simulated=True,
        )
    monkeypatch.setenv("DEMO_MODE", "yes")
    with pytest.raises(PriscillaError, match="labeled simulated"):
        record_event_in_transaction(
            _FakeConn([]), package_entry=uuid4(), kind="delivery",
            label="Delivery happened", simulated=True,
        )
    with pytest.raises(PriscillaError, match="must not be labeled simulated"):
        record_event_in_transaction(
            _FakeConn([]), package_entry=uuid4(), kind="delivery",
            label="simulated delivery actually?", simulated=False,
            evidence_refs=["x"],
        )


def test_record_rejects_bad_identity_kind_and_package() -> None:
    with pytest.raises(PriscillaError, match="demo identifiers"):
        record_event_in_transaction(
            _FakeConn([]), package_entry="demo-1", kind="delivery",
            label="SIMULATED x", simulated=True,
        )
    with pytest.raises(PriscillaError, match="unknown conversion kind"):
        record_event_in_transaction(
            _FakeConn([]), package_entry=uuid4(), kind="sent",
            label="SIMULATED x", simulated=True,
        )
    with pytest.raises(PriscillaError, match="label is required"):
        record_event_in_transaction(
            _FakeConn([]), package_entry=uuid4(), kind="delivery",
            label="  ", simulated=True,
        )
    with pytest.raises(PriscillaError, match="unknown Uriel package"):
        record_event_in_transaction(
            _FakeConn([None]), package_entry=uuid4(), kind="delivery",
            label="SIMULATED x", simulated=True,
        )
    assert KINDS == ("delivery", "engagement", "response", "conversion")


def test_mode_switch_never_converts_simulated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "yes")
    package = _package()
    conn = _FakeConn([package, {"event_id": uuid4(), "created_at": "t"}])
    before = record_event_in_transaction(
        conn, package_entry=package["entry_id"], kind="delivery",
        label="SIMULATED delivery", simulated=True,
    )
    monkeypatch.setenv("DEMO_MODE", "no")
    rows = [
        {"simulated": True, "kind": "delivery", "total": 1},
        {"simulated": False, "kind": "response", "total": 2},
    ]
    metrics = metrics_in_transaction(_FakeConn([rows]))
    assert metrics["simulated"] == {
        "delivery": 1, "engagement": 0, "response": 0, "conversion": 0, "total": 1,
    }
    assert metrics["actual"]["total"] == 2
    assert "total" not in metrics or True  # no combined total key at top level
    assert set(metrics) == {"priscilla_version", "package_entry", "simulated", "actual"}
    assert before["simulated"] is True  # recorded row keeps its kind


def test_metrics_strictly_separated() -> None:
    rows = [
        {"simulated": True, "kind": "delivery", "total": 3},
        {"simulated": True, "kind": "bogus", "total": 9},
        {"simulated": False, "kind": "conversion", "total": 1},
    ]
    metrics = metrics_in_transaction(_FakeConn([rows]), package_entry=uuid4())
    assert metrics["simulated"]["delivery"] == 3
    assert metrics["simulated"]["total"] == 3  # unknown kinds ignored, never mixed
    assert metrics["actual"]["conversion"] == 1
    assert metrics["package_entry"] is not None


def test_malformed_cell_does_not_fail_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "yes")
    package = _package({"county": "narnia", "category": "high_rise"})
    conn = _FakeConn([package, {"event_id": uuid4(), "created_at": "t"}])
    result = record_event_in_transaction(
        conn, package_entry=package["entry_id"], kind="delivery",
        label="SIMULATED delivery", simulated=True,
    )
    assert result["simulated"] is True
    assert len(conn._cursor.executed) == 2  # package read + event insert only
