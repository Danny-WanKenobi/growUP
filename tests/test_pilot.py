"""36-cell pilot ledger acceptance (migrations 013/014).

Local tests run the real pilot code against an in-memory emulator;
live tests (``GROWUP_PG_INTEGRATION=1``) verify real PostgreSQL behavior
inside rolled-back transactions. Nothing here executes discovery or
spends budget.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import uuid4

import pytest

from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER
from app.pilot import (
    PILOT_CATEGORIES,
    TARGET_QUALIFIED_PER_CELL,
    PilotError,
    all_cells_in_transaction,
    canonical_qualified_counts_in_transaction,
    canonical_quota_id,
    cell_from_dossier,
    check_weights,
    default_weights,
    ensure_ledger_in_transaction,
    get_state_in_transaction,
    normalize_category,
    normalize_county,
    pilot_report_in_transaction,
    pilot_success_evidenced,
    plan_next_in_transaction,
    record_transition_in_transaction,
    resume_pilot_in_transaction,
    set_weights_in_transaction,
    stop_pilot_in_transaction,
)

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")

_FAKE_BUDGET = {"used": 90, "ceiling": 100, "remaining": 10, "month": "2026-10"}


class _FakeCursor:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db
        self._result: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def fetchone(self) -> Any:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[Any]:
        return list(self._result)

    def execute(self, sql: str, params: Any = None) -> None:
        p = dict(params or {})
        db = self._db
        if sql.startswith("INSERT INTO pilot_state"):
            if "state" not in db:
                db["state"] = {
                    "status": "stopped",
                    "weights": p["weights"],
                    "limits": p["limits"],
                }
            self._result = []
        elif sql.startswith("INSERT INTO pilot_cells"):
            db.setdefault("cells", {}).setdefault(
                (p["county"], p["category"]),
                {
                    "county": p["county"],
                    "category": p["category"],
                    "target_qualified": p["target"],
                    "discovered": 0,
                    "qualified": 0,
                    "assessed": 0,
                    "authorized": 0,
                    "incomplete": 0,
                    "rejected": 0,
                    "michael_ready": 0,
                    "engineering_assessed": 0,
                    "financially_assessed": 0,
                    "package_prepared": 0,
                    "tracked_actual": 0,
                    "tracked_simulated": 0,
                },
            )
            self._result = []
        elif sql.startswith("SELECT status, weights, limits FROM pilot_state"):
            state = db.get("state")
            self._result = [dict(state)] if state else []
        elif sql.startswith("UPDATE pilot_state SET weights"):
            if "state" in db:
                db["state"]["weights"] = p["weights"]
                self.rowcount = 1
            else:
                self.rowcount = 0
            self._result = []
        elif sql.startswith("UPDATE pilot_state SET status"):
            if "state" in db:
                db["state"]["status"] = p["status"]
                self.rowcount = 1
            else:
                self.rowcount = 0
            self._result = []
        elif sql.startswith("SELECT county FROM pilot_cells"):
            cell = db.get("cells", {}).get((p["county"], p["category"]))
            self._result = [{"county": p["county"]}] if cell else []
        elif sql.startswith("INSERT INTO pilot_events"):
            seen = db.setdefault("events", set())
            key = (str(p["dossier"]), p["transition"])
            if key in seen:
                self._result = []
            else:
                seen.add(key)
                db.setdefault("event_rows", []).append(
                    {
                        "county": p["county"],
                        "category": p["category"],
                        "dossier_id": str(p["dossier"]),
                        "transition": p["transition"],
                        "run_id": str(p.get("run")) if p.get("run") else None,
                        "record_key": p.get("record_key"),
                        "identity_kind": "canonical",
                    }
                )
                self._result = [{"event_id": p["eid"]}]
        elif sql.startswith("SELECT county, category, COUNT(DISTINCT dossier_id)"):
            grouped: dict[tuple[str, str], set[str]] = {}
            for row in db.get("event_rows", []):
                if row["transition"] != "qualified":
                    continue
                if row["identity_kind"] != "canonical":
                    continue
                grouped.setdefault((row["county"], row["category"]), set()).add(
                    row["dossier_id"]
                )
            self._result = [
                {"county": county, "category": category, "total": len(ids)}
                for (county, category), ids in sorted(grouped.items())
            ]
        elif sql.startswith("UPDATE pilot_cells SET"):
            column = sql.split("SET", 1)[1].split("=")[0].strip()
            cell = db["cells"][(p["county"], p["category"])]
            cell[column] += 1
            self._result = []
        elif sql.startswith("SELECT county, category, target_qualified"):
            self._result = [dict(cell) for cell in db.get("cells", {}).values()]
        else:  # pragma: no cover - emulator must cover every pilot statement
            raise AssertionError(f"unhandled SQL: {sql[:80]}")


class _FakeConn:
    def __init__(self) -> None:
        self.db: dict[str, Any] = {}

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.db)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_default_weights_are_equal() -> None:
    assert default_weights() == {
        "high_rise": 1.0,
        "commercial_property": 1.0,
        "warehouse_industrial": 1.0,
    }


def test_check_weights_rejects_bad_shapes() -> None:
    with pytest.raises(PilotError):
        check_weights({"high_rise": 1.0})
    with pytest.raises(PilotError):
        check_weights({**default_weights(), "other": 1.0})
    with pytest.raises(PilotError):
        check_weights({**default_weights(), "high_rise": 0.0})
    with pytest.raises(PilotError):
        check_weights({**default_weights(), "high_rise": -2.0})
    with pytest.raises(PilotError):
        check_weights({**default_weights(), "high_rise": "lots"})
    cleaned = check_weights(
        {"high_rise": 2, "commercial_property": 1.5, "warehouse_industrial": 1}
    )
    assert cleaned == {"high_rise": 2.0, "commercial_property": 1.5, "warehouse_industrial": 1.0}


def test_normalize_county_and_category() -> None:
    assert normalize_county("dallas county") == "dallas county"
    assert normalize_county("Dallas County") == "dallas county"
    assert normalize_category("high_rise") == "high_rise"
    with pytest.raises(PilotError):
        normalize_county("atlantis county")
    with pytest.raises(PilotError):
        normalize_category("skyscraper")


def test_ensure_ledger_creates_36_cells_stopped() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    ensure_ledger_in_transaction(conn)
    state = get_state_in_transaction(conn)
    assert state["status"] == "stopped"
    assert state["weights"] == default_weights()
    cells = all_cells_in_transaction(conn)
    assert len(cells) == 36
    assert cells[0].county == COUNTY_PRIORITY_ORDER[0]
    assert {cell.category for cell in cells} == set(PILOT_CATEGORIES)
    assert all(cell.target_qualified == TARGET_QUALIFIED_PER_CELL for cell in cells)


def test_state_requires_initialized_ledger() -> None:
    with pytest.raises(PilotError, match="not initialized"):
        get_state_in_transaction(_FakeConn())


def test_canonical_quota_id_matches_custody() -> None:
    from app.custody import dossier_id_for_property

    prop = uuid4()
    assert canonical_quota_id(prop) == dossier_id_for_property(prop)
    assert canonical_quota_id(str(prop)) == dossier_id_for_property(prop)
    assert canonical_quota_id(uuid4()) != canonical_quota_id(prop)
    with pytest.raises(PilotError):
        canonical_quota_id("demo-1")
    with pytest.raises(PilotError):
        canonical_quota_id("not-a-uuid")


def _qualified_dossier(
    conn: _FakeConn, county: str, category: str, run_id: Any = None
) -> Any:
    quota = canonical_quota_id(uuid4())
    assert record_transition_in_transaction(
        conn, county=county, category=category, dossier_id=quota,
        transition="qualified", run_id=run_id, record_key="main",
    ) is True
    return quota


def test_cross_run_rediscovery_records_once() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    prop = uuid4()
    quota = canonical_quota_id(prop)
    first = record_transition_in_transaction(
        conn, county="dallas county", category="high_rise",
        dossier_id=quota, transition="qualified",
        run_id=uuid4(), record_key="main",
    )
    second = record_transition_in_transaction(
        conn, county="dallas county", category="high_rise",
        dossier_id=quota, transition="qualified",
        run_id=uuid4(), record_key="main",
    )
    assert (first, second) == (True, False)
    counts = canonical_qualified_counts_in_transaction(conn)
    assert counts == {("dallas county", "high_rise"): 1}
    rows = [row for row in conn.db["event_rows"] if row["transition"] == "qualified"]
    assert len(rows) == 1
    assert rows[0]["identity_kind"] == "canonical"
    assert rows[0]["record_key"] == "main"


def test_cell_from_dossier_requires_verified_county() -> None:
    verified = {
        "county_attribution": {"evidence_status": "verified", "value": "Dallas County"},
        "category_attribution": {"status": "observed", "observed": "high_rise"},
    }
    assert cell_from_dossier(verified) == ("dallas county", "high_rise")
    assert cell_from_dossier({}) is None
    assert cell_from_dossier({"county_attribution": {"evidence_status": "observed"}}) is None
    assert cell_from_dossier({
        "county_attribution": {"evidence_status": "verified", "value": "Dallas County"},
        "category_attribution": {"status": "contradictory"},
    }) is None
    assert cell_from_dossier({
        "county_attribution": {"evidence_status": "verified", "value": "Atlantis County"},
        "category_attribution": {"status": "observed", "observed": "high_rise"},
    }) is None
    assert cell_from_dossier("not-a-dict") is None


def test_new_stage_transitions_record_and_dedup() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    event = uuid4()
    for transition in (
        "engineering_assessed", "financially_assessed", "package_prepared",
        "tracked_actual", "tracked_simulated",
    ):
        assert record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id=event, transition=transition,
        ) is True
        assert record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id=event, transition=transition,
        ) is False  # replay cannot inflate
    report = pilot_report_in_transaction(conn, budget=dict(_FAKE_BUDGET))
    for transition in (
        "engineering_assessed", "financially_assessed", "package_prepared",
        "tracked_actual", "tracked_simulated",
    ):
        assert report["totals"][transition] == 1


def test_pilot_success_requires_four_distinct_per_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.pilot as pilot_module

    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    monkeypatch.setattr(pilot_module, "get_connection", lambda config=None: conn)
    assert pilot_success_evidenced() is False
    # 144 concentrated in one cell does not satisfy the pilot.
    for _ in range(144):
        _qualified_dossier(conn, "dallas county", "high_rise", uuid4())
    assert pilot_success_evidenced() is False
    # One short of a full cell still fails.
    conn2 = _FakeConn()
    ensure_ledger_in_transaction(conn2)
    monkeypatch.setattr(pilot_module, "get_connection", lambda config=None: conn2)
    from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER

    for county in COUNTY_PRIORITY_ORDER:
        for category in PILOT_CATEGORIES:
            count = 3 if (county, category) == ("dallas county", "high_rise") else 4
            for _ in range(count):
                _qualified_dossier(conn2, county, category, uuid4())
    assert pilot_success_evidenced() is False
    _qualified_dossier(conn2, "dallas county", "high_rise", uuid4())
    assert pilot_success_evidenced() is True

    def _boom(config: Any = None) -> Any:
        raise pilot_module.DatabaseNotConfiguredError("no db")

    monkeypatch.setattr(pilot_module, "get_connection", _boom)
    assert pilot_success_evidenced() is False  # unevidenced, not failed


def test_run_scoped_legacy_events_never_count() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    conn.db["event_rows"] = [
        {
            "county": "dallas county",
            "category": "high_rise",
            "dossier_id": str(uuid4()),
            "transition": "qualified",
            "run_id": None,
            "record_key": None,
            "identity_kind": "run_scoped",
        }
        for _ in range(4)
    ]
    assert canonical_qualified_counts_in_transaction(conn) == {}


def test_record_counts_once_and_replays_dedup() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    dossier = uuid4()
    assert record_transition_in_transaction(
        conn, county="dallas county", category="high_rise",
        dossier_id=dossier, transition="discovered",
    ) is True
    assert record_transition_in_transaction(
        conn, county="dallas county", category="high_rise",
        dossier_id=dossier, transition="discovered",
    ) is False
    assert record_transition_in_transaction(
        conn, county="dallas county", category="high_rise",
        dossier_id=dossier, transition="qualified",
    ) is True
    report = pilot_report_in_transaction(conn, budget=dict(_FAKE_BUDGET))
    assert report["totals"]["discovered"] == 1
    assert report["totals"]["qualified"] == 1


def test_record_rejects_demo_bad_uuid_and_unknown_cells() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    with pytest.raises(PilotError, match="never pilot-counted"):
        record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id="demo-1", transition="discovered",
        )
    with pytest.raises(PilotError, match="operational UUID"):
        record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id="nope", transition="discovered",
        )
    with pytest.raises(PilotError, match="unknown transition"):
        record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id=uuid4(), transition="teleported",
        )
    with pytest.raises(PilotError, match="unknown pilot cell"):
        record_transition_in_transaction(
            _FakeConn(), county="dallas county", category="high_rise",
            dossier_id=uuid4(), transition="discovered",
        )


def test_recording_allowed_while_stopped_but_planning_refused() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    assert record_transition_in_transaction(
        conn, county="dallas county", category="high_rise",
        dossier_id=uuid4(), transition="discovered",
    ) is True
    with pytest.raises(PilotError, match="stopped"):
        plan_next_in_transaction(conn, budget=dict(_FAKE_BUDGET))


def test_stop_resume_preserves_ledger() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    assert resume_pilot_in_transaction(conn) == "running"
    record_transition_in_transaction(
        conn, county="dallas county", category="high_rise",
        dossier_id=uuid4(), transition="qualified",
    )
    assert stop_pilot_in_transaction(conn) == "stopped"
    with pytest.raises(PilotError, match="stopped"):
        plan_next_in_transaction(conn, budget=dict(_FAKE_BUDGET))
    assert resume_pilot_in_transaction(conn) == "running"
    report = pilot_report_in_transaction(conn, budget=dict(_FAKE_BUDGET))
    assert report["totals"]["qualified"] == 1
    assert report["shortfall"] == 144 - 1


def test_plan_orders_by_shortfall_then_priority() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    resume_pilot_in_transaction(conn)
    for _ in range(3):
        record_transition_in_transaction(
            conn, county="tarrant county", category="high_rise",
            dossier_id=uuid4(), transition="qualified",
        )
    for _ in range(2):
        record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id=uuid4(), transition="qualified",
        )
    plan = plan_next_in_transaction(conn, max_actions=2, budget=dict(_FAKE_BUDGET))
    assert plan["open_cells"] == 36
    first, second = plan["actions"]
    # Untouched cells (shortfall 4) outrank partial cells; Dallas leads
    # on county priority, and its untouched categories come first.
    assert first["shortfall"] == 4
    assert (first["county"], first["category"]) == ("dallas county", "commercial_property")
    assert (second["county"], second["category"]) == ("dallas county", "warehouse_industrial")


def test_plan_honors_configured_weights_on_ties() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    resume_pilot_in_transaction(conn)
    set_weights_in_transaction(
        conn,
        {"high_rise": 1.0, "commercial_property": 1.0, "warehouse_industrial": 9.0},
    )
    plan = plan_next_in_transaction(conn, max_actions=1, budget=dict(_FAKE_BUDGET))
    assert plan["actions"][0]["category"] == "warehouse_industrial"


def test_plan_flags_over_budget_actions_without_spending() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    resume_pilot_in_transaction(conn)
    tight = {"used": 99, "ceiling": 100, "remaining": 1, "month": "2026-10"}
    plan = plan_next_in_transaction(conn, max_actions=3, budget=tight)
    assert [action["budget_ok"] for action in plan["actions"]] == [False, False, False]
    assert all(action["est_brave_requests"] == 3 for action in plan["actions"])
    with pytest.raises(PilotError):
        plan_next_in_transaction(conn, max_actions=0, budget=dict(_FAKE_BUDGET))


def test_report_shortfalls_are_truthful_per_cell() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    dossier = uuid4()
    for transition in ("discovered", "assessed", "authorized", "incomplete", "rejected"):
        record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id=dossier, transition=transition,
        )
    report = pilot_row(report_cells(conn), "dallas county", "high_rise")
    # Non-qualified stages never reduce the qualified shortfall.
    assert report["shortfall"] == 4
    assert report["discovered"] == 1 and report["authorized"] == 1
    totals = pilot_report_in_transaction(conn, budget=dict(_FAKE_BUDGET))
    assert totals["shortfall"] == 144
    assert totals["target_qualified"] == 144


def report_cells(conn: _FakeConn) -> list[dict[str, Any]]:
    return pilot_report_in_transaction(conn, budget=dict(_FAKE_BUDGET))["rows"]


def pilot_row(rows: list[dict[str, Any]], county: str, category: str) -> dict[str, Any]:
    return next(row for row in rows if row["county"] == county and row["category"] == category)


def test_qualified_completes_a_cell() -> None:
    conn = _FakeConn()
    ensure_ledger_in_transaction(conn)
    for _ in range(4):
        record_transition_in_transaction(
            conn, county="wise county", category="warehouse_industrial",
            dossier_id=uuid4(), transition="qualified",
        )
    row = pilot_row(report_cells(conn), "wise county", "warehouse_industrial")
    assert row["shortfall"] == 0
    resume_pilot_in_transaction(conn)
    plan = plan_next_in_transaction(conn, max_actions=36, budget=dict(_FAKE_BUDGET))
    assert plan["open_cells"] == 35
    assert all(
        (action["county"], action["category"]) != ("wise county", "warehouse_industrial")
        for action in plan["actions"]
    )


def test_cli_status_plan_stop_resume(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    import app.pilot as pilot_module

    conn = _FakeConn()
    monkeypatch.setattr(pilot_module, "get_connection", lambda config=None: conn)
    monkeypatch.setattr(pilot_module, "read_budget", lambda config=None: dict(_FAKE_BUDGET))
    ensure_ledger_in_transaction(conn)
    assert pilot_module.main(["status"]) == 0
    assert "qualified: 0/144" in capsys.readouterr().out
    assert pilot_module.main(["plan"]) == 1  # stopped
    assert pilot_module.main(["resume"]) == 0
    assert pilot_module.main(["plan", "--max", "2"]) == 0
    assert pilot_module.main(["stop"]) == 0
    assert pilot_module.main(["bogus"]) == 1
    assert pilot_module.main(["plan", "--max", "nope"]) == 1


def test_cli_database_failures_map_to_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.pilot as pilot_module
    from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError

    def _missing(config: Any = None) -> Any:
        raise DatabaseNotConfiguredError("database_not_configured")

    def _down(config: Any = None) -> Any:
        raise DatabaseUnavailableError("database_error")

    monkeypatch.setattr(pilot_module, "get_connection", _missing)
    assert pilot_module.main(["status"]) == 2
    monkeypatch.setattr(pilot_module, "get_connection", _down)
    assert pilot_module.main(["status"]) == 3


@needs_live_pg
def test_live_pilot_roundtrip_rolls_back() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.pilot_state') AS s, "
                "to_regclass('public.pilot_cells') AS c, "
                "to_regclass('public.pilot_events') AS e"
            )
            row = cur.fetchone()
    if row is None or row["s"] is None or row["c"] is None or row["e"] is None:
        pytest.skip("pilot tables not present (migration 013 not applied)")

    conn = get_connection()
    try:
        ensure_ledger_in_transaction(conn)
        state = get_state_in_transaction(conn)
        assert state["status"] == "stopped"
        dossier = uuid4()
        assert record_transition_in_transaction(
            conn, county="Dallas County", category="high_rise",
            dossier_id=dossier, transition="discovered",
        ) is True
        assert record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id=dossier, transition="discovered",
        ) is False
        record_transition_in_transaction(
            conn, county="dallas county", category="high_rise",
            dossier_id=dossier, transition="qualified",
        )
        with pytest.raises(PilotError, match="stopped"):
            plan_next_in_transaction(conn, budget=dict(_FAKE_BUDGET))
        resume_pilot_in_transaction(conn)
        plan = plan_next_in_transaction(conn, max_actions=36, budget=dict(_FAKE_BUDGET))
        assert plan["open_cells"] == 36  # one qualified does not close a cell
        report = pilot_report_in_transaction(conn, budget=dict(_FAKE_BUDGET))
        assert report["totals"]["qualified"] == 1
        assert report["shortfall"] == 143
    finally:
        conn.rollback()
        conn.close()
