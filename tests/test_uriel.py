"""Uriel package preparation tests (fake connections, no PG)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from app.uriel import (
    PACKAGE_STATES,
    ROLE_SCHEMA,
    URIEL_PACKAGE_VERSION,
    UrielError,
    get_package_in_transaction,
    list_packages_in_transaction,
    main,
    prepare_package_in_transaction,
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


def _receipt(**overrides: Any) -> dict[str, Any]:
    row = {
        "receipt_id": uuid4(),
        "dossier_id": uuid4(),
        "version": 2,
        "snapshot_id": uuid4(),
        "kind": "forward",
        "from_agent": "michael",
        "to_agent": "uriel",
    }
    row.update(overrides)
    return row


def _prepare(conn: _FakeConn, receipt: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return prepare_package_in_transaction(
        conn,
        dossier_id=receipt["dossier_id"],
        version=receipt["version"],
        receipt_id=receipt["receipt_id"],
        prepared_by="op-1",
        **kwargs,
    )


def test_prepare_draft_without_review() -> None:
    receipt = _receipt()
    conn = _FakeConn([receipt, None, {"entry_id": uuid4(), "created_at": "t"}])
    result = _prepare(conn, receipt, unknowns=["roof age"], next_actions=["ask owner"])
    assert result["package_state"] == "draft"
    assert result["simulated"] is False
    assert result["payload"]["package_version"] == URIEL_PACKAGE_VERSION
    assert result["payload"]["lineage"]["receipt_from"] == "michael"
    assert result["payload"]["review"] == {}
    insert_sql, insert_params = next(
        (sql, params) for sql, params in conn._cursor.executed
        if "INSERT INTO uriel_packages" in sql
    )
    assert insert_params["schema"] == ROLE_SCHEMA == "canonical1-uriel"
    assert insert_params["state"] == "draft"
    assert insert_sql


def test_prepare_reviewed_with_blockers() -> None:
    receipt = _receipt()
    conn = _FakeConn([receipt, None, {"entry_id": uuid4(), "created_at": "t"}])
    result = _prepare(
        conn, receipt, blockers=["missing roof age"],
        review={"reviewed_by": "op-2", "note": "checked finances"},
    )
    assert result["package_state"] == "reviewed"


def test_prepare_prepared_with_clean_review() -> None:
    receipt = _receipt()
    conn = _FakeConn([receipt, None, {"entry_id": uuid4(), "created_at": "t"}])
    result = _prepare(conn, receipt, review={"reviewed_by": "op-2", "note": "ok"})
    assert result["package_state"] == "prepared"


def test_prepare_rejects_custody_mismatch() -> None:
    receipt = _receipt(to_agent="priscilla")
    conn = _FakeConn([receipt])
    with pytest.raises(UrielError, match="does not hand custody to Uriel"):
        _prepare(conn, receipt)
    receipt = _receipt()
    conn = _FakeConn([receipt])
    with pytest.raises(UrielError, match="different dossier version"):
        prepare_package_in_transaction(
            conn, dossier_id=receipt["dossier_id"], version=99,
            receipt_id=receipt["receipt_id"], prepared_by="op-1",
        )


def test_prepare_rejects_unknown_receipt_and_duplicates() -> None:
    conn = _FakeConn([None])
    with pytest.raises(UrielError, match="unknown custody receipt"):
        prepare_package_in_transaction(
            conn, dossier_id=uuid4(), version=1, receipt_id=uuid4(),
            prepared_by="op-1",
        )
    receipt = _receipt()
    conn = _FakeConn([receipt, {"entry_id": uuid4()}])
    with pytest.raises(UrielError, match="already prepared"):
        _prepare(conn, receipt)


def test_prepare_rejects_demo_and_bad_identity() -> None:
    conn = _FakeConn([])
    with pytest.raises(UrielError, match="demo identifiers"):
        prepare_package_in_transaction(
            conn, dossier_id="demo-1", version=1, receipt_id=uuid4(),
            prepared_by="op-1",
        )
    with pytest.raises(UrielError, match="operational UUID"):
        prepare_package_in_transaction(
            conn, dossier_id="not-a-uuid", version=1, receipt_id=uuid4(),
            prepared_by="op-1",
        )
    receipt = _receipt()
    conn = _FakeConn([receipt])
    with pytest.raises(UrielError, match="prepared_by"):
        prepare_package_in_transaction(
            conn, dossier_id=receipt["dossier_id"], version=receipt["version"],
            receipt_id=receipt["receipt_id"], prepared_by="  ",
        )


def test_prepare_records_demo_mode_but_stays_genuine(monkeypatch: pytest.MonkeyPatch) -> None:
    for mode in ("yes", "no"):
        monkeypatch.setenv("DEMO_MODE", mode)
        receipt = _receipt()
        conn = _FakeConn([receipt, None, {"entry_id": uuid4(), "created_at": "t"}])
        result = _prepare(conn, receipt)
        assert result["simulated"] is False
        assert result["payload"]["demo_mode"] is (mode == "yes")
        assert not any("UPDATE uriel_packages" in sql for sql, _ in conn._cursor.executed)


def test_prepare_records_pilot_transition_for_known_cell() -> None:
    receipt = _receipt()
    conn = _FakeConn([
        receipt, None, {"entry_id": uuid4(), "created_at": "t"},
        {"county": "dallas"}, {"event_id": uuid4()},
    ])
    dossier = {
        "county_attribution": {"evidence_status": "verified", "value": "Dallas County"},
        "category_attribution": {"status": "observed", "observed": "high_rise"},
    }
    result = _prepare(conn, receipt, dossier=dossier)
    assert result["payload"]["cell"] == {"county": "dallas county", "category": "high_rise"}
    assert any("INSERT INTO pilot_events" in sql for sql, _ in conn._cursor.executed)


def test_prepare_skips_pilot_for_unknown_cell() -> None:
    receipt = _receipt()
    conn = _FakeConn([receipt, None, {"entry_id": uuid4(), "created_at": "t"}])
    result = _prepare(conn, receipt, dossier={"county_attribution": {"evidence_status": "observed"}})
    assert result["payload"]["cell"] is None
    assert not any("pilot_events" in sql for sql, _ in conn._cursor.executed)


def test_duplicate_race_maps_to_uriel_error() -> None:
    psycopg = pytest.importorskip("psycopg")

    class _RaceCursor(_FakeCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            super().execute(sql, params)
            if "INSERT INTO uriel_packages" in sql:
                err = psycopg.Error("duplicate key")
                err.sqlstate = "23505"  # type: ignore[attr-defined]
                raise err

    class _RaceConn(_FakeConn):
        def __init__(self, script: list[Any]) -> None:
            self._cursor = _RaceCursor(script)

    receipt = _receipt()
    conn = _RaceConn([receipt, None])
    with pytest.raises(UrielError, match="already prepared"):
        _prepare(conn, receipt)


def _package_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "entry_id": uuid4(),
        "dossier_id": uuid4(),
        "version": 2,
        "snapshot_id": uuid4(),
        "receipt_id": uuid4(),
        "demo_mode": True,
        "simulated": False,
        "payload": json.dumps({"package_version": URIEL_PACKAGE_VERSION}),
        "role_schema": ROLE_SCHEMA,
        "package_state": "prepared",
        "execution_state": "not_attempted",
        "created_at": "2026-10-30T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def test_get_package_decodes_payload() -> None:
    conn = _FakeConn([_package_row()])
    record = get_package_in_transaction(conn, uuid4())
    assert record is not None
    assert record["payload"]["package_version"] == URIEL_PACKAGE_VERSION
    assert record["execution_state"] == "not_attempted"
    assert get_package_in_transaction(_FakeConn([None]), uuid4()) is None


def test_list_packages_filters_and_validates() -> None:
    rows = [_package_row(), _package_row(package_state="draft")]
    listed = list_packages_in_transaction(_FakeConn([rows]))
    assert [row["package_state"] for row in listed] == ["prepared", "draft"]
    listed = list_packages_in_transaction(_FakeConn([rows[:1]]), package_state="prepared")
    assert len(listed) == 1
    with pytest.raises(UrielError, match="unknown package state"):
        list_packages_in_transaction(_FakeConn([]), package_state="queued")
    with pytest.raises(UrielError, match="limit"):
        list_packages_in_transaction(_FakeConn([]), limit=0)
    assert PACKAGE_STATES == ("draft", "reviewed", "prepared")


def test_cli_list_and_show(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    import app.uriel as uriel_module

    rows = [{"entry_id": "e1", "dossier_id": "d1", "version": 2,
             "package_state": "prepared", "created_at": "t"}]
    monkeypatch.setattr(uriel_module, "_owned", lambda fn, *a, **k: rows)
    assert main(["list"]) == 0
    assert "state=prepared" in capsys.readouterr().out
    monkeypatch.setattr(
        uriel_module, "_owned",
        lambda fn, *a, **k: {"entry_id": "e1", "payload": {}},
    )
    assert main(["show", str(uuid4())]) == 0
    assert '"entry_id": "e1"' in capsys.readouterr().out
    monkeypatch.setattr(uriel_module, "_owned", lambda fn, *a, **k: None)
    assert main(["show", str(uuid4())]) == 1

    def _raise(fn: Any, *a: Any, **k: Any) -> Any:
        raise UrielError("unknown package state: 'bogus'")

    monkeypatch.setattr(uriel_module, "_owned", _raise)
    assert main(["list", "--state", "bogus"]) == 1
    assert main([]) == 2
