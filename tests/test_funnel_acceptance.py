"""Funnel acceptance: real coordinator chain + Wondrue entry points.

One repeatable scenario drives the actual coordinator
(Gabriel finalize -> custody -> Raphael -> Onesimus/Michael ->
Uriel -> approved Priscilla simulation) against a stateful fake,
then inspects the results through the real Wondrue handlers.
A PostgreSQL-gated test replays the same chain against the
migration-created schema (rollback-only, disposable).
Disposable data only; no external calls.
"""

from __future__ import annotations

import json
import os
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.custody import dossier_id_for_property


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

    # -- helpers ------------------------------------------------------
    def _records(self) -> list[dict[str, Any]]:
        return self._db["records"]

    def execute(self, sql: str, params: Any = None) -> None:
        p = dict(params or {})
        db = self._db
        # Workflow runs ------------------------------------------------
        if sql.startswith("SELECT * FROM workflow_runs WHERE run_id"):
            row = next(
                (r for r in db["runs"] if str(r["run_id"]) == str(p["rid"])), None
            )
            self._result = [dict(row)] if row else []
        elif sql.startswith("UPDATE workflow_runs SET stage"):
            updated = 0
            for row in db["runs"]:
                if str(row["run_id"]) == str(p["rid"]) and row["stage"] == p["expected"]:
                    row["stage"] = p["next"]
                    row["status"] = p["status"]
                    updated = 1
            self.rowcount = updated
            self._result = []
        # Workflow records ---------------------------------------------
        elif sql.startswith("INSERT INTO workflow_records"):
            key = (str(p["run"]), p["kind"], p["key"], p["version"])
            if key in db["record_keys"]:
                self._result = []
            else:
                db["record_keys"].add(key)
                row = {
                    "record_id": p["rid"], "run_id": p["run"], "kind": p["kind"],
                    "record_key": p["key"], "version": p["version"],
                    "payload": json.loads(p["payload"]), "digest": p["digest"],
                }
                db["records"].append(row)
                self._result = [dict(row)]
        elif sql.startswith("SELECT * FROM workflow_records WHERE run_id"):
            rows = [r for r in db["records"] if str(r["run_id"]) == str(p["run"])]
            if "AND kind = %(kind)s" in sql:
                rows = [r for r in rows if r["kind"] == p["kind"]]
            if "AND record_key = %(key)s" in sql:
                rows = [r for r in rows if r["record_key"] == p["key"]]
            if "AND version = %(version)s" in sql:
                rows = [r for r in rows if r["version"] == p["version"]]
            if "ORDER BY version DESC LIMIT 1" in sql:
                rows.sort(key=lambda r: r["version"], reverse=True)
                rows = rows[:1]
            elif "ORDER BY record_key, version" in sql:
                rows.sort(key=lambda r: (r["record_key"], r["version"]))
            elif "ORDER BY kind, record_key, version" in sql:
                rows.sort(key=lambda r: (r["kind"], r["record_key"], r["version"]))
            self._result = [dict(r) for r in rows]
        # Pilot ledger --------------------------------------------------
        elif sql.startswith("INSERT INTO pilot_state"):
            db["pilot_state"] = {
                "status": "stopped", "weights": {}, "limits": {},
            }
            self._result = []
        elif sql.startswith("INSERT INTO pilot_cells"):
            key = (p["county"], p["category"])
            if key not in db["cells"]:
                db["cells"][key] = {
                    "county": p["county"], "category": p["category"],
                    "target_qualified": p["target"],
                    "discovered": 0, "qualified": 0, "assessed": 0,
                    "authorized": 0, "engineering_assessed": 0,
                    "financially_assessed": 0, "package_prepared": 0,
                    "tracked_actual": 0, "tracked_simulated": 0,
                    "incomplete": 0, "rejected": 0,
                }
                self._result = [{"county": p["county"]}]
            else:
                self._result = []
        elif sql.startswith("SELECT county FROM pilot_cells"):
            key = (p["county"], p["category"])
            self._result = [{"county": p["county"]}] if key in db["cells"] else []
        elif sql.startswith("INSERT INTO pilot_events"):
            key = (str(p["dossier"]), p["transition"])
            if key in db["pilot_seen"]:
                self._result = []
            else:
                db["pilot_seen"].add(key)
                db["pilot_events"].append(
                    {
                        "county": p["county"], "category": p["category"],
                        "dossier_id": str(p["dossier"]), "transition": p["transition"],
                        "run_id": str(p.get("run")) if p.get("run") else None,
                        "record_key": p.get("record_key"),
                        "identity_kind": "canonical",
                    }
                )
                self._result = [{"event_id": p["eid"]}]
        elif sql.startswith("UPDATE pilot_cells SET"):
            key = (p["county"], p["category"])
            column = sql.split("SET ", 1)[1].split(" =", 1)[0].strip()
            if key in db["cells"]:
                db["cells"][key][column] += 1
            self._result = []
        elif sql.startswith("SELECT county, category, COUNT(DISTINCT dossier_id)"):
            grouped: dict[tuple[str, str], set[str]] = {}
            for row in db["pilot_events"]:
                if row["transition"] != "qualified" or row["identity_kind"] != "canonical":
                    continue
                grouped.setdefault((row["county"], row["category"]), set()).add(
                    row["dossier_id"]
                )
            self._result = [
                {"county": c, "category": g, "total": len(ids)}
                for (c, g), ids in sorted(grouped.items())
            ]
        # Custody --------------------------------------------------------
        elif sql.startswith("SELECT pg_advisory_xact_lock"):
            self._result = [{"pg_advisory_xact_lock": None}]
        elif sql.startswith("SELECT COALESCE(MAX(version), 0)"):
            versions = [
                r["version"] for r in db["versions"]
                if str(r["dossier_id"]) == str(p["dossier"])
            ]
            self._result = [{"max_version": max(versions, default=0)}]
        elif sql.startswith("INSERT INTO dossier_versions"):
            row = {
                "dossier_id": p["dossier"], "version": p["version"],
                "property_id": p["property"],
                "payload": json.loads(p["payload"]), "digest": p["digest"],
                "supersedes": p["supersedes"],
                "amendment": json.loads(p["amendment"]) if p["amendment"] else None,
                "created_by": p["created_by"],
            }
            db["versions"].append(row)
            self._result = [dict(row)]
        elif sql.startswith("SELECT * FROM dossier_versions"):
            rows = [
                r for r in db["versions"]
                if str(r["dossier_id"]) == str(p["dossier"])
            ]
            if "AND version = %(version)s" in sql:
                rows = [r for r in rows if r["version"] == p["version"]]
            else:
                rows.sort(key=lambda r: r["version"], reverse=True)
                rows = rows[:1]
            self._result = [dict(r) for r in rows]
        elif sql.startswith("INSERT INTO custody_snapshots"):
            key = (str(p["dossier"]), p["version"])
            if key in db["snapshot_keys"]:
                self._result = []
            else:
                db["snapshot_keys"].add(key)
                row = {
                    "snapshot_id": p["sid"], "dossier_id": p["dossier"],
                    "version": p["version"],
                    "payload": json.loads(p["payload"]), "digest": p["digest"],
                }
                db["snapshots"].append(row)
                self._result = [dict(row)]
        elif sql.startswith("SELECT * FROM custody_snapshots WHERE dossier_id"):
            row = next(
                (
                    r for r in db["snapshots"]
                    if str(r["dossier_id"]) == str(p["dossier"])
                    and r["version"] == p["version"]
                ),
                None,
            )
            self._result = [dict(row)] if row else []
        elif sql.startswith("SELECT * FROM custody_snapshots WHERE snapshot_id"):
            row = next(
                (r for r in db["snapshots"] if str(r["snapshot_id"]) == str(p["sid"])),
                None,
            )
            self._result = [dict(row)] if row else []
        elif "custody_snapshots_archive" in sql:
            self._result = []
        elif sql.startswith("INSERT INTO custody_receipts"):
            row = {
                "receipt_id": p["rid"], "dossier_id": p["dossier"],
                "version": p["version"], "snapshot_id": p["snapshot"],
                "kind": p["kind"], "from_agent": p["from"], "to_agent": p["to"],
                "payload": json.loads(p["payload"]), "mac": p["mac"],
                "prev_receipt": p["prev"], "pipeline_version": p["pipeline"],
                "created_at": f"t{len(db['receipts'])}",
            }
            db["receipts"].append(row)
            self._result = [dict(row)]
        elif sql.startswith("SELECT * FROM custody_receipts WHERE dossier_id"):
            # Production orders by predecessor links; return every row in
            # adversarial timestamp/UUID order so link-ordering is exercised.
            rows = [
                r for r in db["receipts"]
                if str(r["dossier_id"]) == str(p["dossier"])
            ]
            rows.sort(key=lambda r: (r["created_at"], str(r["receipt_id"])), reverse=True)
            self._result = [dict(r) for r in rows]
        elif sql.startswith("SELECT receipt_id, dossier_id, version, snapshot_id"):
            row = next(
                (r for r in db["receipts"] if str(r["receipt_id"]) == str(p["rid"])),
                None,
            )
            self._result = [dict(row)] if row else []
        elif sql.startswith("SELECT * FROM custody_receipts WHERE receipt_id"):
            row = next(
                (r for r in db["receipts"] if str(r["receipt_id"]) == str(p["rid"])),
                None,
            )
            self._result = [dict(row)] if row else []
        # Uriel ----------------------------------------------------------
        elif sql.startswith("SELECT entry_id FROM uriel_packages"):
            rows = [
                r for r in db["packages"]
                if str(r["dossier_id"]) == str(p["dossier"]) and r["version"] == p["version"]
            ]
            self._result = [{"entry_id": r["entry_id"]} for r in rows]
        elif sql.startswith("INSERT INTO uriel_packages"):
            payload = json.loads(p["payload"])
            row = {
                "entry_id": p["eid"], "dossier_id": p["dossier"],
                "version": p["version"], "snapshot_id": p["snapshot"],
                "receipt_id": p["receipt"], "prepared_by": payload.get("prepared_by"),
                "demo_mode": p["demo"], "package_state": p["state"],
                "simulated": False, "execution_state": "not_attempted",
                "role_schema": p["schema"],
                "payload": payload, "created_at": "t",
            }
            db["packages"].append(row)
            self._result = [{"entry_id": p["eid"], "created_at": "t"}]
        elif "FROM uriel_packages" in sql and "entry_id = ANY" in sql:
            wanted = {str(i) for i in p["ids"]}
            rows = [r for r in db["packages"] if str(r["entry_id"]) in wanted]
            rows.sort(key=lambda r: str(r["entry_id"]))
            self._result = [dict(r) for r in rows]
        elif "FROM uriel_packages" in sql and "WHERE entry_id" in sql:
            row = next(
                (r for r in db["packages"] if str(r["entry_id"]) == str(p["eid"])),
                None,
            )
            self._result = [dict(row)] if row else []
        elif "FROM uriel_packages" in sql and "LIMIT" in sql:
            rows = sorted(db["packages"], key=lambda r: str(r["entry_id"]), reverse=True)
            self._result = [dict(r) for r in rows[: p["limit"]]]
        elif "FROM uriel_packages ORDER BY entry_id" in sql:
            rows = sorted(db["packages"], key=lambda r: str(r["entry_id"]))
            self._result = [dict(r) for r in rows]
        # Priscilla simulations ------------------------------------------
        elif sql.startswith("INSERT INTO priscilla_simulations"):
            if p["key"] in db["sims"]:
                # ON CONFLICT DO NOTHING: existing row wins, no error raised.
                self._result = []
                return
            db["sims"][p["key"]] = {
                "sim_id": p["sim"], "idempotency_key": p["key"],
                "cohort": p["cohort"], "package_ids": list(p["packages"]),
                "dossier_refs": json.loads(p["refs"]),
                "config_version": p["config"],
                "response_rate": p["response_rate"],
                "customer_rate": p["customer_rate"], "rounding": p["rounding"],
                "selection_method": p["method"], "seed": p["seed"],
                "actor": p["actor"], "status": "completed",
                "counts": json.loads(p["counts"]),
                "exclusions": json.loads(p["exclusions"]),
                "created_at": "t0", "completed_at": "t1",
            }
            self._result = [
                {"sim_id": p["sim"], "created_at": "t0", "completed_at": "t1"}
            ]
        elif "FROM priscilla_simulations" in sql and "WHERE idempotency_key" in sql:
            row = db["sims"].get(p["key"])
            self._result = [dict(row)] if row else []
        elif "FROM priscilla_simulations" in sql and "WHERE sim_id" in sql:
            row = next(
                (r for r in db["sims"].values() if str(r["sim_id"]) == str(p["sim"])),
                None,
            )
            self._result = [dict(row)] if row else []
        # Conversion events -----------------------------------------------
        elif sql.startswith("INSERT INTO conversion_events"):
            if p.get("simulation") is not None:
                key = (str(p["simulation"]), str(p["entry"]), p["kind"])
                if key in db["sim_keys"]:
                    self._result = []
                    return
                db["sim_keys"].add(key)
            db["conversions"].append(
                {
                    "event_id": p["eid"], "package_entry": p["entry"],
                    "kind": p["kind"], "simulated": p["simulated"],
                    "label": p["label"], "payload": json.loads(p["payload"]),
                    "simulation_id": p.get("simulation"), "created_at": "t",
                }
            )
            self._result = [{"event_id": p["eid"], "created_at": "t"}]
        elif sql.startswith("SELECT event_id, created_at FROM conversion_events"):
            row = next(
                (
                    r for r in db["conversions"]
                    if str(r["simulation_id"]) == str(p["simulation"])
                    and str(r["package_entry"]) == str(p["entry"])
                    and r["kind"] == p["kind"]
                ),
                None,
            )
            self._result = [dict(row)] if row else []
        elif sql.startswith("SELECT package_entry, kind, simulated FROM conversion_events"):
            if "simulation_id = " in sql:
                rows = [
                    r for r in db["conversions"]
                    if str(r["simulation_id"]) == str(p["sim"])
                ]
            else:
                rows = list(db["conversions"])
            self._result = [dict(r) for r in rows]
        elif sql.startswith("SELECT simulated, kind, COUNT(*)"):
            grouped: dict[tuple[bool, str], int] = {}
            for row in db["conversions"]:
                if "WHERE package_entry" in sql and str(row["package_entry"]) != str(
                    p["entry"]
                ):
                    continue
                key = (bool(row["simulated"]), row["kind"])
                grouped[key] = grouped.get(key, 0) + 1
            self._result = [
                {"simulated": sim, "kind": kind, "total": total}
                for (sim, kind), total in sorted(grouped.items(), key=str)
            ]
        elif sql.startswith("SELECT package_entry, kind FROM conversion_events"):
            wanted = {str(i) for i in p["ids"]}
            self._result = [
                dict(r) for r in db["conversions"]
                if str(r["package_entry"]) in wanted
                and not r["simulated"] and r["simulation_id"] is None
            ]
        else:  # pragma: no cover - fake must cover the whole chain
            raise AssertionError(f"unhandled SQL: {sql[:100]}")


class _FakeConn:
    def __init__(self, db: dict[str, Any]) -> None:
        self._db = db

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._db)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _new_db() -> dict[str, Any]:
    return {
        "runs": [], "records": [], "record_keys": set(),
        "cells": {}, "pilot_state": None,
        "pilot_seen": set(), "pilot_events": [],
        "versions": [], "snapshots": [], "snapshot_keys": set(), "receipts": [],
        "packages": [], "conversions": [], "sim_keys": set(), "sims": {},
    }


def _dossier_payload(prop: UUID) -> dict[str, Any]:
    return {
        "version": 1,
        "builder_version": "t",
        "artifact_refs": ["a1"],
        "property_id": str(prop),
        "dossier": {
            "identity_disputes": {},
            "conflicts": [],
            "category_attribution": {"status": "observed", "observed": "high_rise"},
            "county_attribution": {
                "evidence_status": "verified", "value": "Dallas County",
            },
            "sources": ["https://broker-a.example/x", "https://broker-b.example/y"],
        },
        "candidate_verification": {
            "outcome": "VERIFIED",
            "pathway": "CREDIBLE_CORROBORATION",
            "reasons": [],
        },
    }


def _site_inputs() -> dict[str, Any]:
    return {
        "usable_area_m2": {"value": 500.0},
        "price_per_kg": {"value": 8.0},
        "nutrient_usd_per_l": {"value": 0.05},
        "electricity_usd_per_kwh": {"value": 0.12},
        "water_usd_per_l": {"value": 0.002},
        "labor_h_per_m2_week": {"value": 0.1},
        "labor_usd_per_h": {"value": 15.0},
        "hvac_usd_per_m2_year": {"value": 8.0},
        "other_opex_usd_per_m2_year": {"value": 5.0},
    }


def _seed_run(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], UUID, UUID]:
    from app.pilot import ensure_ledger_in_transaction
    from app.workflow import digest_payload
    from app.workflow_db import put_record_in_transaction

    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "acceptance-test-key-0123456789")
    monkeypatch.setenv("WONDRUE_OPERATOR_KEY", "operator-secret")
    monkeypatch.setenv("WONDRUE_READER_KEY", "reader-secret")
    db = _new_db()
    conn = _FakeConn(db)
    run_id = uuid4()
    prop = uuid4()
    db["runs"].append(
        {
            "run_id": run_id, "idempotency_key": "accept-1",
            "stage": "gabriel", "status": "open", "correlation_id": "",
            "limits": {}, "usage": {}, "errors": [],
        }
    )
    payload = _dossier_payload(prop)
    put_record_in_transaction(
        conn, run_id, kind="dossier_version", record_key="main",
        version=1, payload=payload, digest=digest_payload(payload),
    )
    ensure_ledger_in_transaction(conn)
    return db, run_id, prop


def test_full_chain_coordinator_then_wondrue(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import mvp as mvp_module
    from app.pilot import canonical_qualified_counts_in_transaction
    from app.wondrue import handle_command

    db, run_id, prop = _seed_run(monkeypatch)
    conn = _FakeConn(db)
    summary = mvp_module.evaluate_real(
        conn, run_id, "a1", "q", site_inputs=_site_inputs(),
        reviewed_by="owner", run_simulation=True, simulation_key="accept-1",
    )
    assert summary["stage"] == "done"
    assert summary["decision"] == "authorized"
    assert set(summary["scenarios"]) == {"bench", "rack3"}
    assert all(item["complete"] for item in summary["scenarios"].values())
    assert all(
        item["financial_complete"] for item in summary["scenarios"].values()
    )
    assert summary["package"]["package_state"] == "prepared"
    assert summary["simulation"]["counts"]["cohort"] == 1
    assert summary["simulation"]["counts"]["responses"] == 0
    dossier_id = dossier_id_for_property(prop)
    assert summary["dossier_id"] == str(dossier_id)

    # Custody chain is complete and holder-linked.
    chain = [(r["kind"], r["from_agent"], r["to_agent"]) for r in db["receipts"]]
    assert chain == [
        ("forward", "gabriel", "raphael"),
        ("forward", "raphael", "onesimus"),
        ("forward", "onesimus", "michael"),
        ("forward", "michael", "uriel"),
    ]
    assert len(db["versions"]) == 1 and len(db["snapshots"]) == 1

    # Package is genuine: never synthetic, never executed.
    package = db["packages"][0]
    assert package["simulated"] is False
    assert package["execution_state"] == "not_attempted"
    assert package["payload"]["review"]["reviewed_by"] == "owner"

    # Simulation rows are explicitly simulated and provenance-bound.
    assert all(row["simulated"] is True for row in db["conversions"])
    assert all("simulated" in row["label"] for row in db["conversions"])
    assert len(db["conversions"]) == 1  # one delivery for the N=1 cohort

    # Pilot ledger reflects actual processing under canonical identity.
    transitions = sorted(
        (row["transition"], row["dossier_id"]) for row in db["pilot_events"]
    )
    kinds = sorted(row["transition"] for row in db["pilot_events"])
    assert kinds == [
        "assessed", "authorized", "engineering_assessed",
        "financially_assessed", "package_prepared", "tracked_simulated",
    ]
    assert {dossier for _, dossier in transitions} == {str(dossier_id)}
    assert canonical_qualified_counts_in_transaction(conn) == {}

    # Wondrue entry points inspect the same persisted results.
    override = lambda intent, args=None: {"intent": intent, "args": args or {}}
    packages = handle_command(
        mode="read-only", max_mode="read-only", text="", target="real",
        intent_override=override("inspect_packages"), conn=conn,
    )
    assert len(packages["data"]["packages"]) == 1
    preview = handle_command(
        mode="read-only", max_mode="read-only", text="", target="real",
        intent_override=override("preview_simulation", {"cohort": "mvp"}), conn=conn,
    )
    assert preview["data"]["expected"]["cohort"] == 1
    sim_id = summary["simulation"]["sim_id"]
    inspected = handle_command(
        mode="read-only", max_mode="read-only", text="", target="real",
        intent_override=override("inspect_simulation", {"simulation_id": sim_id}),
        conn=conn,
    )
    funnel = inspected["data"]["funnel"]
    assert funnel["simulated_unique"] == {"outreach": 1, "responses": 0, "customers": 0}
    assert funnel["actual_unique"] == {
        "delivery": 0, "engagement": 0, "response": 0, "conversion": 0,
    }
    assert funnel["subset_consistent"] is True

    # Re-running a finished run duplicates nothing.
    counts_before = (
        len(db["versions"]), len(db["receipts"]), len(db["packages"]),
        len(db["conversions"]), len(db["pilot_events"]),
    )
    again = mvp_module.evaluate_real(conn, run_id, "a1", "q")
    assert again["stage"] == "done"
    counts_after = (
        len(db["versions"]), len(db["receipts"]), len(db["packages"]),
        len(db["conversions"]), len(db["pilot_events"]),
    )
    assert counts_before == counts_after


def test_mode_switch_cannot_send_or_relabel(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import mvp as mvp_module
    from app.priscilla import metrics_in_transaction
    from app.wondrue import handle_command

    db, run_id, _prop = _seed_run(monkeypatch)
    conn = _FakeConn(db)
    mvp_module.evaluate_real(
        conn, run_id, "a1", "q", site_inputs=_site_inputs(),
        reviewed_by="owner", run_simulation=True, simulation_key="accept-2",
    )
    before = [(r["simulated"], r["label"]) for r in db["conversions"]]
    monkeypatch.setenv("DEMO_MODE", "no")
    # Metrics stay separated; nothing is relabeled by the switch.
    metrics = metrics_in_transaction(conn)
    assert metrics["simulated"]["delivery"] == 1
    assert metrics["actual"]["delivery"] == 0
    assert [(r["simulated"], r["label"]) for r in db["conversions"]] == before
    assert db["packages"][0]["execution_state"] == "not_attempted"
    # New simulations are refused in no-mode.
    with pytest.raises(Exception, match="DEMO_MODE=yes"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            intent_override={
                "intent": "run_simulation", "args": {"cohort": "late"},
            },
            conn=conn,
        )


_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


@needs_live_pg
def test_live_full_chain_coordinator_then_wondrue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real PG: the coordinator chain + Wondrue inspects, rollback-only.

    The run starts at the gabriel stage with a persisted dossier, so
    no acquisition or other external call can occur. Fixture data
    exercises the authorize branch on the disposable database only.
    """
    from app import mvp as mvp_module
    from app.pilot import ensure_ledger_in_transaction
    from app.properties_db import get_connection
    from app.workflow import digest_payload
    from app.workflow_db import (
        create_run_in_transaction,
        put_record_in_transaction,
    )
    from app.wondrue import handle_command

    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")
    monkeypatch.setenv("DEMO_MODE", "yes")
    monkeypatch.setenv("CUSTODY_HMAC_KEY", "acceptance-test-key-0123456789")
    monkeypatch.setenv("WONDRUE_OPERATOR_KEY", "operator-secret")
    monkeypatch.setenv("WONDRUE_READER_KEY", "reader-secret")
    conn = get_connection()
    try:
        prop = uuid4()
        run = create_run_in_transaction(
            conn, idempotency_key=f"live-accept-{uuid4().hex}",
            stage="gabriel",
        )
        run_id = run.run_id
        payload = _dossier_payload(prop)
        put_record_in_transaction(
            conn, run_id, kind="dossier_version", record_key="main",
            version=1, payload=payload, digest=digest_payload(payload),
        )
        ensure_ledger_in_transaction(conn)
        summary = mvp_module.evaluate_real(
            conn, run_id, "a1", "q", site_inputs=_site_inputs(),
            reviewed_by="owner", run_simulation=True,
            simulation_key=f"live-accept-{uuid4().hex}",
        )
        assert summary["stage"] == "done"
        assert summary["decision"] == "authorized"
        assert set(summary["scenarios"]) == {"bench", "rack3"}
        assert summary["package"]["package_state"] == "prepared"
        assert summary["simulation"]["counts"]["cohort"] == 1
        dossier_id = dossier_id_for_property(prop)
        assert summary["dossier_id"] == str(dossier_id)

        def _count(sql: str, params: dict[str, Any]) -> int:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return int((cur.fetchone() or {})["total"])

        versions = _count(
            "SELECT COUNT(*) AS total FROM dossier_versions "
            "WHERE dossier_id = %(d)s",
            {"d": dossier_id},
        )
        receipts = _count(
            "SELECT COUNT(*) AS total FROM custody_receipts "
            "WHERE dossier_id = %(d)s",
            {"d": dossier_id},
        )
        packages = _count(
            "SELECT COUNT(*) AS total FROM uriel_packages "
            "WHERE dossier_id = %(d)s",
            {"d": dossier_id},
        )
        conversions = _count(
            "SELECT COUNT(*) AS total FROM conversion_events "
            "WHERE simulation_id = %(s)s",
            {"s": UUID(str(summary["simulation"]["sim_id"]))},
        )
        transitions = _count(
            "SELECT COUNT(*) AS total FROM pilot_events "
            "WHERE dossier_id = %(d)s",
            {"d": dossier_id},
        )
        assert (versions, receipts, packages, conversions) == (1, 4, 1, 1)
        assert transitions == 6

        with conn.cursor() as cur:
            cur.execute(
                "SELECT receipt_id, prev_receipt, kind, from_agent, to_agent "
                "FROM custody_receipts WHERE dossier_id = %(d)s",
                {"d": dossier_id},
            )
            fetched = cur.fetchall()
        # Chain order comes from predecessor links: timestamps tie
        # within one transaction and UUIDs carry no order.
        assert len(fetched) == 4
        roots = [row for row in fetched if row["prev_receipt"] is None]
        assert len(roots) == 1
        chain = []
        cursor_row: dict[str, Any] | None = roots[0]
        while cursor_row is not None:
            chain.append(
                (
                    cursor_row["kind"],
                    cursor_row["from_agent"],
                    cursor_row["to_agent"],
                )
            )
            followers = [
                row for row in fetched
                if str(row["prev_receipt"]) == str(cursor_row["receipt_id"])
            ]
            assert len(followers) <= 1
            cursor_row = followers[0] if followers else None
        assert chain == [
            ("forward", "gabriel", "raphael"),
            ("forward", "raphael", "onesimus"),
            ("forward", "onesimus", "michael"),
            ("forward", "michael", "uriel"),
        ]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT simulated, execution_state FROM uriel_packages "
                "WHERE dossier_id = %(d)s",
                {"d": dossier_id},
            )
            package = cur.fetchone()
            assert package["simulated"] is False
            assert package["execution_state"] == "not_attempted"
            cur.execute(
                "SELECT simulated, label FROM conversion_events "
                "WHERE simulation_id = %(s)s",
                {"s": UUID(str(summary["simulation"]["sim_id"]))},
            )
            events = cur.fetchall()
            assert len(events) == 1
            assert events[0]["simulated"] is True
            assert "simulated" in events[0]["label"]

        override = lambda intent, args=None: {
            "intent": intent, "args": args or {},
        }
        entry_id = summary["package"]["entry_id"]
        inspected_packages = handle_command(
            mode="read-only", max_mode="read-only", text="", target="real",
            intent_override=override("inspect_packages"), conn=conn,
        )
        assert entry_id in [
            item["entry_id"]
            for item in inspected_packages["data"]["packages"]
        ]
        preview = handle_command(
            mode="read-only", max_mode="read-only", text="", target="real",
            intent_override=override(
                "preview_simulation",
                {"cohort": "mvp", "package_entries": [entry_id]},
            ),
            conn=conn,
        )
        assert preview["data"]["expected"]["cohort"] == 1
        inspected = handle_command(
            mode="read-only", max_mode="read-only", text="", target="real",
            intent_override=override(
                "inspect_simulation",
                {"simulation_id": summary["simulation"]["sim_id"]},
            ),
            conn=conn,
        )
        funnel = inspected["data"]["funnel"]
        assert funnel["simulated_unique"] == {
            "outreach": 1, "responses": 0, "customers": 0,
        }
        assert funnel["subset_consistent"] is True

        # Re-running a finished run duplicates nothing.
        mvp_module.evaluate_real(conn, run_id, "a1", "q")
        assert _count(
            "SELECT COUNT(*) AS total FROM dossier_versions "
            "WHERE dossier_id = %(d)s",
            {"d": dossier_id},
        ) == versions
        assert _count(
            "SELECT COUNT(*) AS total FROM custody_receipts "
            "WHERE dossier_id = %(d)s",
            {"d": dossier_id},
        ) == receipts
        assert _count(
            "SELECT COUNT(*) AS total FROM uriel_packages "
            "WHERE dossier_id = %(d)s",
            {"d": dossier_id},
        ) == packages
        assert _count(
            "SELECT COUNT(*) AS total FROM conversion_events "
            "WHERE simulation_id = %(s)s",
            {"s": UUID(str(summary["simulation"]["sim_id"]))},
        ) == conversions
        assert _count(
            "SELECT COUNT(*) AS total FROM pilot_events "
            "WHERE dossier_id = %(d)s",
            {"d": dossier_id},
        ) == transitions
    finally:
        conn.rollback()
        conn.close()
