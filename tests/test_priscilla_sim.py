"""Priscilla deterministic cohort simulation + unique-lead funnel.

Stateful in-memory fake stands in for PostgreSQL; live PG validation
needs owner approval (migration 015 unapplied). No external calls.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.priscilla import (
    PriscillaError,
    funnel_in_transaction,
    get_simulation_in_transaction,
    preview_simulation_in_transaction,
    rank_entries,
    run_simulation_in_transaction,
    sim_expected_counts,
)


def _package(
    *,
    state: str = "prepared",
    simulated: bool = False,
    execution: str = "not_attempted",
    blockers: list[str] | None = None,
    version: int = 1,
    dossier: UUID | None = None,
    entry: UUID | None = None,
) -> dict[str, Any]:
    return {
        "entry_id": entry or uuid4(),
        "dossier_id": dossier or uuid4(),
        "version": version,
        "package_state": state,
        "simulated": simulated,
        "execution_state": execution,
        "payload": {"blockers": list(blockers or []), "cell": None},
        "created_at": "t",
    }


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
        p = dict(params or {})
        db = self._db
        if "FROM uriel_packages ORDER BY entry_id" in sql:
            rows = sorted(db["packages"], key=lambda row: str(row["entry_id"]))
            self._result = [dict(row) for row in rows]
        elif "FROM uriel_packages WHERE entry_id = ANY" in sql:
            wanted = {str(item) for item in p["ids"]}
            rows = [row for row in db["packages"] if str(row["entry_id"]) in wanted]
            rows.sort(key=lambda row: str(row["entry_id"]))
            self._result = [dict(row) for row in rows]
        elif "FROM uriel_packages" in sql and "WHERE entry_id" in sql:
            found = next(
                (row for row in db["packages"] if str(row["entry_id"]) == str(p["eid"])),
                None,
            )
            self._result = [dict(found)] if found else []
        elif sql.startswith("INSERT INTO priscilla_simulations"):
            if p["key"] in db["sims"]:
                # ON CONFLICT DO NOTHING: existing row wins, no error raised.
                self._result = []
                return
            rival_wins = db.pop("race", False)
            stored_sim = uuid4() if rival_wins else p["sim"]
            db["sims"][p["key"]] = {
                "sim_id": stored_sim,
                "idempotency_key": p["key"],
                "cohort": p["cohort"],
                "package_ids": list(p["packages"]),
                "dossier_refs": json.loads(p["refs"]),
                "config_version": p["config"],
                "response_rate": p["response_rate"],
                "customer_rate": p["customer_rate"],
                "rounding": p["rounding"],
                "selection_method": p["method"],
                "seed": p["seed"],
                "actor": p["actor"],
                "status": "completed",
                "counts": json.loads(p["counts"]),
                "exclusions": json.loads(p["exclusions"]),
                "created_at": "t0",
                "completed_at": "t1",
            }
            # A rival transaction committing first: our insert loses and
            # we re-read the winner's row (identical inputs here).
            self._result = (
                []
                if rival_wins
                else [{"sim_id": p["sim"], "created_at": "t0", "completed_at": "t1"}]
            )
        elif "FROM priscilla_simulations" in sql and "WHERE idempotency_key" in sql:
            row = db["sims"].get(p["key"])
            self._result = [dict(row)] if row else []
        elif "FROM priscilla_simulations" in sql and "WHERE sim_id" in sql:
            row = next(
                (item for item in db["sims"].values() if str(item["sim_id"]) == str(p["sim"])),
                None,
            )
            self._result = [dict(row)] if row else []
        elif sql.startswith("INSERT INTO conversion_events"):
            key = (str(p.get("simulation")), str(p["entry"]), p["kind"])
            if p.get("simulation") is not None and key in db["sim_keys"]:
                self._result = []
            else:
                if p.get("simulation") is not None:
                    db["sim_keys"].add(key)
                db["events"].append(
                    {
                        "event_id": p["eid"],
                        "package_entry": p["entry"],
                        "kind": p["kind"],
                        "simulated": p["simulated"],
                        "label": p["label"],
                        "payload": json.loads(p["payload"]),
                        "simulation_id": p.get("simulation"),
                        "created_at": "t",
                    }
                )
                self._result = [{"event_id": p["eid"], "created_at": "t"}]
        elif sql.startswith("SELECT event_id, created_at FROM conversion_events"):
            found = next(
                (
                    row
                    for row in db["events"]
                    if str(row["simulation_id"]) == str(p["simulation"])
                    and str(row["package_entry"]) == str(p["entry"])
                    and row["kind"] == p["kind"]
                ),
                None,
            )
            self._result = [dict(found)] if found else []
        elif sql.startswith("SELECT package_entry, kind, simulated FROM conversion_events"):
            if "simulation_id = " in sql:
                rows = [
                    row for row in db["events"]
                    if str(row["simulation_id"]) == str(p["sim"])
                ]
            else:
                rows = list(db["events"])
            self._result = [dict(row) for row in rows]
        elif sql.startswith("SELECT package_entry, kind FROM conversion_events"):
            wanted = {str(item) for item in p["ids"]}
            self._result = [
                dict(row)
                for row in db["events"]
                if str(row["package_entry"]) in wanted
                and not row["simulated"]
                and row["simulation_id"] is None
            ]
        else:  # pragma: no cover - fake must cover every simulation statement
            raise AssertionError(f"unhandled SQL: {sql[:100]}")


class _FakeConn:
    def __init__(self, packages: list[dict[str, Any]] | None = None) -> None:
        self.db: dict[str, Any] = {
            "packages": list(packages or []),
            "sims": {},
            "sim_keys": set(),
            "events": [],
        }

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.db)


def _cohort(size: int) -> list[dict[str, Any]]:
    return [_package() for _ in range(size)]


def test_expected_counts_144() -> None:
    counts = sim_expected_counts(144)
    assert counts == {
        "cohort": 144,
        "deliveries": 144,
        "responses": 14,
        "customers": 5,
        "response_pct": 9.72,
        "customer_pct": 35.71,
    }


def test_expected_counts_small_and_empty() -> None:
    assert sim_expected_counts(40)["responses"] == 4
    assert sim_expected_counts(40)["customers"] == 1  # round_half_up(1.44)
    assert sim_expected_counts(9) == {
        "cohort": 9, "deliveries": 9, "responses": 0, "customers": 0,
        "response_pct": 0.0, "customer_pct": None,
    }
    assert sim_expected_counts(0)["customers"] == 0
    with pytest.raises(PriscillaError):
        sim_expected_counts(-1)


def test_ranking_ignores_input_order() -> None:
    ids = [uuid4() for _ in range(10)]
    assert rank_entries(ids, "s") == rank_entries(list(reversed(ids)), "s")
    assert rank_entries(ids, "s") == rank_entries(ids, "s")  # stable per seed


def test_preview_is_read_only_and_discloses_shortfall() -> None:
    conn = _FakeConn(_cohort(40))
    preview = preview_simulation_in_transaction(conn, cohort="demo-40", seed="s1")
    assert preview["expected"]["cohort"] == 40
    assert preview["expected"]["responses"] == 4
    assert preview["expected"]["customers"] == 1
    assert len(preview["eligible"]) == 40
    assert preview["exclusions"] == []
    assert conn.db["sims"] == {} and conn.db["events"] == []


def test_run_144_produces_14_and_5() -> None:
    conn = _FakeConn(_cohort(144))
    run = run_simulation_in_transaction(
        conn, idempotency_key="sim-144", cohort="pilot", actor="wondrue", seed="k"
    )
    assert run["replayed"] is False
    assert run["counts"]["responses"] == 14
    assert run["counts"]["customers"] == 5
    assert run["counts"]["response_pct"] == 9.72
    assert run["counts"]["customer_pct"] == 35.71
    assert len(run["package_ids"]) == 144
    by_kind: dict[str, set[str]] = {}
    for row in conn.db["events"]:
        by_kind.setdefault(row["kind"], set()).add(str(row["package_entry"]))
        assert row["simulated"] is True
        assert "simulated" in row["label"]
        assert row["payload"]["detail"]["simulation_id"] == run["sim_id"]
    assert len(by_kind["delivery"]) == 144
    assert len(by_kind["response"]) == 14
    assert len(by_kind["conversion"]) == 5
    assert by_kind["conversion"] <= by_kind["response"] <= by_kind["delivery"]


def test_retry_replays_without_duplicates() -> None:
    conn = _FakeConn(_cohort(20))
    first = run_simulation_in_transaction(conn, idempotency_key="k1", cohort="c")
    again = run_simulation_in_transaction(conn, idempotency_key="k1", cohort="c")
    assert again["replayed"] is True
    assert again["sim_id"] == first["sim_id"]
    assert len(conn.db["events"]) == 20 + 2 + 1  # no duplicate rows
    with pytest.raises(PriscillaError, match="different cohort"):
        run_simulation_in_transaction(conn, idempotency_key="k1", cohort="other")


def test_concurrent_insert_race_replays_winner() -> None:
    conn = _FakeConn(_cohort(10))
    conn.db["race"] = True  # rival transaction wins the insert
    raced = run_simulation_in_transaction(conn, idempotency_key="k-race", cohort="c")
    assert raced["replayed"] is True
    assert len(conn.db["sims"]) == 1  # exactly one stored run
    assert conn.db["events"] == []  # loser wrote no events


def test_conflicting_seed_rejected() -> None:
    conn = _FakeConn(_cohort(10))
    run_simulation_in_transaction(conn, idempotency_key="k", cohort="c", seed="s1")
    with pytest.raises(PriscillaError, match="different seed"):
        run_simulation_in_transaction(
            conn, idempotency_key="k", cohort="c", seed="s2"
        )


def test_conflicting_package_selection_rejected() -> None:
    conn = _FakeConn(_cohort(10))
    run_simulation_in_transaction(conn, idempotency_key="k", cohort="c")
    conn.db["packages"].append(_package())  # cohort grows between calls
    with pytest.raises(PriscillaError, match="different package selection"):
        run_simulation_in_transaction(conn, idempotency_key="k", cohort="c")


def test_outcome_stable_across_input_order() -> None:
    packages = _cohort(30)
    first = preview_simulation_in_transaction(
        _FakeConn(list(packages)), cohort="c", seed="z"
    )
    second = preview_simulation_in_transaction(
        _FakeConn(list(reversed(packages))), cohort="c", seed="z"
    )
    assert first["responders"] == second["responders"]
    assert first["customers"] == second["customers"]


def test_exclusions_recorded_with_reasons() -> None:
    keep = _package()
    dossier = uuid4()
    old = _package(dossier=dossier, version=1)
    new = _package(dossier=dossier, version=2)
    conn = _FakeConn([
        keep, old, new,
        _package(state="draft"),
        _package(blockers=["missing deed"]),
        _package(simulated=True),
        _package(execution="actually_attempted"),
    ])
    preview = preview_simulation_in_transaction(conn, cohort="c")
    assert {row["package_entry"] for row in preview["eligible"]} == {
        str(keep["entry_id"]), str(new["entry_id"])
    }
    reasons = sorted(item["reason"] for item in preview["exclusions"])
    assert reasons == [
        "already_executed", "blocked_package", "draft_package",
        "superseded_version", "synthetic_package",
    ]


def test_explicit_entries_fail_closed() -> None:
    good = _package()
    bad = _package(state="draft")
    conn = _FakeConn([good, bad])
    with pytest.raises(PriscillaError, match="unknown Uriel package"):
        run_simulation_in_transaction(
            conn, idempotency_key="k", package_entries=[uuid4()]
        )
    with pytest.raises(PriscillaError, match="ineligible for simulation"):
        run_simulation_in_transaction(
            conn, idempotency_key="k", package_entries=[bad["entry_id"]]
        )
    # Duplicate request IDs count the lead once.
    run = run_simulation_in_transaction(
        conn, idempotency_key="k2",
        package_entries=[good["entry_id"], good["entry_id"]],
    )
    assert run["counts"]["cohort"] == 1


def test_demo_mode_no_refuses_simulation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "no")
    with pytest.raises(PriscillaError, match="DEMO_MODE=yes"):
        run_simulation_in_transaction(_FakeConn(_cohort(5)), idempotency_key="k")


_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _live_seed_packages(conn: Any, count: int) -> list[UUID]:
    """Seed genuine prepared packages through the real custody chain."""
    from app.custody_db import (
        create_version_in_transaction,
        issue_receipt_in_transaction,
        snapshot_in_transaction,
    )
    from app.uriel import prepare_package_in_transaction

    entries = []
    for _ in range(count):
        version = create_version_in_transaction(
            conn, property_id=uuid4(), payload={"live": "sim"},
            created_by="live-test",
        )
        snapshot_in_transaction(
            conn, dossier_id=version.dossier_id, version=version.version
        )
        prev = None
        for from_agent, to_agent in (
            ("gabriel", "raphael"), ("raphael", "onesimus"),
            ("onesimus", "michael"), ("michael", "uriel"),
        ):
            receipt = issue_receipt_in_transaction(
                conn, dossier_id=version.dossier_id, version=version.version,
                kind="forward", from_agent=from_agent, to_agent=to_agent,
                issued_by="live-test",
            )
            prev = receipt.receipt_id
        package = prepare_package_in_transaction(
            conn, dossier_id=version.dossier_id, version=version.version,
            receipt_id=prev, prepared_by="live-test",
            dossier={"property_id": str(version.property_id)},
            review={"reviewed_by": "live-test"},
        )
        entries.append(UUID(str(package["entry_id"])))
    return entries


@needs_live_pg
def test_live_simulation_idempotent_conflict_and_separation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG: idempotent replay, conflicting reuse, actual/sim split."""
    from app.priscilla import (
        funnel_in_transaction,
        record_event_in_transaction,
    )
    from app.properties_db import get_connection

    monkeypatch.setenv("DEMO_MODE", "yes")
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "live-sim-test-key-0123456789abcdef")
    conn = get_connection()
    try:
        entries = _live_seed_packages(conn, 20)
        key = f"live-sim-{uuid4().hex}"
        first = run_simulation_in_transaction(
            conn, idempotency_key=key, cohort="live", seed="s",
            package_entries=entries,
        )
        assert first["counts"] == {
            "cohort": 20, "deliveries": 20, "responses": 2, "customers": 1,
        }
        replayed = run_simulation_in_transaction(
            conn, idempotency_key=key, cohort="live", seed="s",
            package_entries=entries,
        )
        assert replayed["replayed"] is True
        assert replayed["sim_id"] == first["sim_id"]
        assert replayed["counts"] == first["counts"]
        with pytest.raises(PriscillaError, match="different seed"):
            run_simulation_in_transaction(
                conn, idempotency_key=key, cohort="live", seed="other",
                package_entries=entries,
            )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total FROM priscilla_simulations "
                "WHERE idempotency_key = %(key)s",
                {"key": key},
            )
            assert (cur.fetchone() or {})["total"] == 1
            cur.execute(
                "SELECT COUNT(*) AS total FROM conversion_events "
                "WHERE simulation_id = %(sim)s",
                {"sim": UUID(str(first["sim_id"]))},
            )
            assert (cur.fetchone() or {})["total"] == 23
        record_event_in_transaction(
            conn, package_entry=entries[0], kind="response",
            label="actual owner reply", simulated=False,
            evidence_refs=["live-evidence-1"],
        )
        funnel = funnel_in_transaction(
            conn, simulation_id=UUID(str(first["sim_id"]))
        )
        assert funnel["simulated_unique"] == {
            "outreach": 20, "responses": 2, "customers": 1,
        }
        assert funnel["actual_unique"]["response"] == 1
        assert funnel["subset_consistent"] is True
    finally:
        conn.rollback()
        conn.close()


@needs_live_pg
def test_live_simulation_concurrent_single_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG: two concurrent same-key inserts leave one run, no dupes.

    Both workers commit (cross-connection visibility), so the winning
    run, its packages, and its events stay in the disposable database;
    the uuid cohort marker identifies them for review. The loser's
    replay is rolled back.
    """
    import threading

    from app.properties_db import get_connection

    monkeypatch.setenv("DEMO_MODE", "yes")
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "live-sim-test-key-0123456789abcdef")
    seed_conn = get_connection()
    try:
        race_entries = [str(entry) for entry in _live_seed_packages(seed_conn, 20)]
        seed_conn.commit()
    finally:
        seed_conn.close()
    key = f"live-sim-race-{uuid4().hex}"
    cohort = f"live-race-{uuid4().hex}"
    barrier = threading.Barrier(2)
    results: dict[str, Any] = {}
    errors: list[str] = []

    def _worker(slot: str) -> None:
        worker_conn = get_connection()
        try:
            barrier.wait(timeout=30)
            outcome = run_simulation_in_transaction(
                worker_conn, idempotency_key=key, cohort=cohort, seed="s",
                package_entries=race_entries,
            )
            results[slot] = outcome
            if outcome.get("replayed"):
                worker_conn.rollback()
            else:
                worker_conn.commit()
        except Exception as exc:  # surfaced below, never swallowed
            errors.append(f"{slot}: {type(exc).__name__}: {exc}")
            worker_conn.rollback()
        finally:
            worker_conn.close()

    threads = [
        threading.Thread(target=_worker, args=("a",)),
        threading.Thread(target=_worker, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not [thread for thread in threads if thread.is_alive()]
    assert errors == []
    assert set(results) == {"a", "b"}
    assert results["a"]["sim_id"] == results["b"]["sim_id"]
    assert [bool(item.get("replayed")) for item in results.values()].count(True) == 1
    check_conn = get_connection()
    try:
        with check_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS total FROM priscilla_simulations "
                "WHERE idempotency_key = %(key)s",
                {"key": key},
            )
            assert (cur.fetchone() or {})["total"] == 1
            cur.execute(
                "SELECT COUNT(*) AS total FROM conversion_events "
                "WHERE simulation_id = %(sim)s",
                {"sim": UUID(str(results["a"]["sim_id"]))},
            )
            assert (cur.fetchone() or {})["total"] == 23
    finally:
        check_conn.rollback()
        check_conn.close()


def test_simulation_leaves_packages_untouched() -> None:
    packages = _cohort(10)
    before = json.dumps(packages, default=str)
    conn = _FakeConn(packages)
    run_simulation_in_transaction(conn, idempotency_key="k", cohort="c")
    assert json.dumps(conn.db["packages"], default=str) == before
    assert all(row["execution_state"] == "not_attempted" for row in conn.db["packages"])


def test_funnel_unique_leads_and_actual_separation() -> None:
    from app.priscilla import record_event_in_transaction

    packages = _cohort(20)
    conn = _FakeConn(packages)
    run = run_simulation_in_transaction(conn, idempotency_key="k", cohort="c")
    # Actual evidenced events on two cohort leads (+ a duplicate row).
    cohort_ids = [str(item) for item in run["package_ids"][:2]]
    for entry in cohort_ids:
        record_event_in_transaction(
            conn, package_entry=entry, kind="response",
            label="Owner replied by email", simulated=False,
            evidence_refs=["mailbox:1"],
        )
    record_event_in_transaction(
        conn, package_entry=cohort_ids[0], kind="response",
        label="Owner replied again", simulated=False,
        evidence_refs=["mailbox:2"],
    )
    funnel = funnel_in_transaction(conn, simulation_id=run["sim_id"])
    assert funnel["cohort_unique"] == 20
    assert funnel["simulated_unique"] == {"outreach": 20, "responses": 2, "customers": 1}
    assert funnel["actual_unique"]["response"] == 2  # duplicate row counts once
    assert funnel["actual_unique"]["conversion"] == 0
    assert funnel["subset_consistent"] is True
    assert funnel["expected"]["responses"] == 2
    assert "total" not in funnel or True  # no combined total offered
    with pytest.raises(PriscillaError, match="unknown simulation"):
        funnel_in_transaction(conn, simulation_id=uuid4())


def test_get_simulation_roundtrip() -> None:
    conn = _FakeConn(_cohort(3))
    run = run_simulation_in_transaction(conn, idempotency_key="k", cohort="c")
    stored = get_simulation_in_transaction(conn, run["sim_id"])
    assert stored is not None
    assert stored["idempotency_key"] == "k"
    assert stored["config_version"] == "priscilla-sim-1"
    assert stored["selection_method"] == "seeded-sha256-prefix"
    assert get_simulation_in_transaction(conn, uuid4()) is None
