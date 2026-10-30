"""Synthetic-harness isolation tests (no database, no network)."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.demo import (
    DEMO_LABEL,
    DemoError,
    DemoStore,
    demo_gabriel_stage,
    demo_id,
    demo_onesimus_stage,
    demo_raphael_stage,
    run_demo_workflow,
)
from app.workflow_db import WorkflowError


def test_demo_run_needs_no_credentials(monkeypatch) -> None:
    for var in (
        "DATABASE_HOST", "DATABASE_PORT", "DATABASE_NAME",
        "DATABASE_USER", "DATABASE_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    store = DemoStore()
    summary = run_demo_workflow(store, demo_id("smoke"))
    assert summary["synthetic"] is True
    assert summary["decision"] == "authorized"
    assert store.runs[demo_id("smoke")]["stage"] == "done"


def test_demo_touches_no_production_connection(monkeypatch) -> None:
    import app.properties_db as properties_db

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("demo must not open production connections")

    monkeypatch.setattr(properties_db, "get_connection", _boom)
    store = DemoStore()
    run_demo_workflow(store, demo_id("noconn"))
    assert store.export_run(demo_id("noconn"))["records"]


def test_demo_sources_are_never_fetched(monkeypatch) -> None:
    import app.agents.gabriel.sources.brave as brave

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("demo must not call providers")

    monkeypatch.setattr(brave, "execute_query", _boom)
    store = DemoStore()
    summary = run_demo_workflow(store, demo_id("nofetch"))
    assert summary["synthetic"] is True


def test_every_demo_artifact_carries_marker_and_label() -> None:
    store = DemoStore()
    run_key = demo_id("marked")
    run_demo_workflow(store, run_key)
    exported = store.export_run(run_key)
    assert exported["synthetic"] is True
    assert exported["demo_label"] == DEMO_LABEL
    assert exported["run"]["synthetic"] is True
    assert exported["run"]["demo_label"] == DEMO_LABEL
    assert len(exported["records"]) >= 5
    for record in exported["records"]:
        assert record["payload"]["synthetic"] is True
        assert record["payload"]["demo_label"] == DEMO_LABEL
        assert record["record_key"].startswith("demo-")


def test_demo_store_rejects_non_demo_keys() -> None:
    store = DemoStore()
    with pytest.raises(DemoError):
        store.create_run("real-run-key")
    store.create_run(demo_id("k"))
    with pytest.raises(DemoError):
        store.put_record(demo_id("k"), "scenario", "real-key", 1, {})


def test_production_storage_rejects_demo_keys() -> None:
    from app.workflow_db import put_record_in_transaction

    class _Conn:
        def cursor(self) -> Any:
            raise AssertionError("must fail before any SQL")

    with pytest.raises(WorkflowError, match="demo identifiers"):
        put_record_in_transaction(
            _Conn(), uuid4(), kind="scenario", record_key=demo_id("x"),
            version=1, payload={}, digest="d",
        )


def test_production_authorization_rejects_demo_keys() -> None:
    from app.agents.raphael.service import authorize_in_transaction

    class _Conn:
        def cursor(self) -> Any:
            raise AssertionError("must fail before any SQL")

    with pytest.raises(WorkflowError, match="demo identifiers"):
        authorize_in_transaction(_Conn(), uuid4(), demo_id("dossier"))


def test_production_acquisition_rejects_demo_assignment() -> None:
    from app.agents.raphael.acquire import acquire_evidence_in_transaction

    class _Conn:
        def cursor(self) -> Any:
            raise AssertionError("must fail before any SQL")

    with pytest.raises(WorkflowError, match="demo identifiers"):
        acquire_evidence_in_transaction(
            _Conn(), uuid4(), demo_id("a1"), "q", approved=True
        )


def test_simulated_decision_cannot_authorize_real_scenario() -> None:
    from app.agents.onesimus.service import run_scenario_in_transaction

    class _Conn:
        def cursor(self) -> Any:
            raise AssertionError("must fail before any SQL")

    store = DemoStore()
    run_key = demo_id("sim")
    dossier = demo_gabriel_stage(store, run_key)
    decision = demo_raphael_stage(store, run_key, dossier["record_key"])
    assert decision["payload"]["simulated"] is True
    with pytest.raises(WorkflowError, match="demo-only"):
        run_scenario_in_transaction(
            _Conn(), uuid4(), scenario_key="s1", config_name="bench",
            crop={}, site={}, decision_digest=decision["digest"],
        )


def test_demo_scenarios_require_simulated_authorized_decision() -> None:
    store = DemoStore()
    run_key = demo_id("gate")
    dossier = demo_gabriel_stage(store, run_key)
    hold = demo_raphael_stage(
        store, run_key, dossier["record_key"], simulated_outcome="needs_evidence"
    )
    with pytest.raises(DemoError, match="simulated authorized"):
        demo_onesimus_stage(store, run_key, hold["record_key"], "bench")
    with pytest.raises(DemoError, match="simulated demo decision"):
        demo_onesimus_stage(store, run_key, demo_id("missing"), "bench")


def test_demo_reset_only_clears_demo_store() -> None:
    store = DemoStore()
    run_demo_workflow(store, demo_id("r1"))
    assert store.runs and store.records
    store.reset()
    assert store.runs == {} and store.records == {}


def test_production_registry_untouched_by_demo() -> None:
    from app.agents.gabriel.source_registry import PRODUCTION_REGISTRY

    store = DemoStore()
    run_demo_workflow(store, demo_id("reg"))
    assert PRODUCTION_REGISTRY.entries == {}
    assert not any("fiction" in host for host in PRODUCTION_REGISTRY.entries)


def test_demo_conflicting_replay_raises() -> None:
    store = DemoStore()
    run_key = demo_id("replay")
    store.create_run(run_key)
    key = demo_id("rec")
    store.put_record(run_key, "scenario", key, 1, {"a": 1})
    same = store.put_record(run_key, "scenario", key, 1, {"a": 1})
    assert same["payload"]["a"] == 1
    with pytest.raises(DemoError, match="conflicting replay"):
        store.put_record(run_key, "scenario", key, 1, {"a": 2})
