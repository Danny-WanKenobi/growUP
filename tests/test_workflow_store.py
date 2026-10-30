"""Workflow-run persistence tests (migration 012).

Local tests cover models, digests, the migration-file contract, and the
idempotency/conflict branches via a fake cursor. Live tests
(``GROWUP_PG_INTEGRATION=1``) verify real PostgreSQL behavior on
isolated synthetic rows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.workflow import (
    DossierVersion,
    ModelExecution,
    Qualification,
    RaphaelDecision,
    ResearchArtifact,
    Scenario,
    WorkflowRecord,
    WorkflowRun,
    digest_payload,
)
from app.workflow_db import (
    WorkflowConflictError,
    WorkflowError,
    advance_stage_in_transaction,
    create_run_in_transaction,
    get_record_in_transaction,
    put_record_in_transaction,
)

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _migration_sql() -> str:
    path = Path(__file__).resolve().parent.parent / "migrations" / "012_workflow_runs.sql"
    return path.read_text(encoding="utf-8")


def test_migration_creates_workflow_tables() -> None:
    sql = _migration_sql()
    assert "CREATE TABLE IF NOT EXISTS workflow_runs" in sql
    assert "CREATE TABLE IF NOT EXISTS workflow_records" in sql
    assert "idempotency_key TEXT NOT NULL UNIQUE" in sql
    assert "UNIQUE (run_id, kind, record_key, version)" in sql
    assert "ALTER TABLE properties" not in sql


def test_digest_is_deterministic() -> None:
    payload = {"b": [1, 2], "a": "x"}
    assert digest_payload(payload) == digest_payload(dict(reversed(list(payload.items()))))
    assert digest_payload(payload) != digest_payload({"a": "y", "b": [1, 2]})
    assert len(digest_payload(payload)) == 64


def test_models_have_safe_defaults() -> None:
    run = WorkflowRun.model_validate({"run_id": str(uuid4()), "idempotency_key": "k"})
    assert run.stage == "created" and run.status == "open"
    assert run.limits == {} and run.usage == {} and run.errors == []
    artifact = ResearchArtifact.model_validate({"provider": "brave"})
    assert artifact.results == [] and artifact.attempt == "completed"
    dossier = DossierVersion.model_validate({})
    assert dossier.version == 1 and dossier.property_id == ""
    qual = Qualification.model_validate({})
    assert qual.outcome == "needs_research" and qual.county_credit == ""
    decision = RaphaelDecision.model_validate({})
    assert decision.outcome == "needs_evidence"
    scenario = Scenario.model_validate({})
    assert scenario.complete is False
    execution = ModelExecution.model_validate({})
    assert execution.mode == "replay"


class _FakeCursor:
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 0

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


def _run_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "run_id": uuid4(),
        "idempotency_key": "key-1",
        "stage": "created",
        "status": "open",
        "correlation_id": "",
        "limits": {},
        "usage": {},
        "errors": [],
    }
    row.update(overrides)
    return row


def _record_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "record_id": uuid4(),
        "run_id": uuid4(),
        "kind": "dossier_version",
        "record_key": "main",
        "version": 1,
        "payload": {"a": 1},
        "digest": "abc",
    }
    row.update(overrides)
    return row


def test_create_run_replays_existing_key() -> None:
    conn = _FakeConn([None, _run_row()])
    run = create_run_in_transaction(conn, idempotency_key="key-1")
    assert run.idempotency_key == "key-1"
    sql = conn._cursor.executed[0][0]
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in sql


def test_create_run_requires_key() -> None:
    with pytest.raises(WorkflowError):
        create_run_in_transaction(_FakeConn([]), idempotency_key="")


def test_advance_stage_returns_rowcount_bool() -> None:
    conn = _FakeConn([])
    conn._cursor.rowcount = 1
    assert advance_stage_in_transaction(conn, uuid4(), "a", "b") is True
    conn2 = _FakeConn([])
    conn2._cursor.rowcount = 0
    assert advance_stage_in_transaction(conn2, uuid4(), "a", "b") is False
    assert "AND stage = " in conn2._cursor.executed[0][0]


def test_put_record_identical_replay_returns_existing() -> None:
    existing = _record_row(digest="abc")
    conn = _FakeConn([None, existing])
    record = put_record_in_transaction(
        conn, existing["run_id"], kind="dossier_version", record_key="main",
        version=1, payload={"a": 1}, digest="abc",
    )
    assert record.record_id == existing["record_id"]


def test_put_record_conflicting_replay_raises() -> None:
    existing = _record_row(digest="abc")
    conn = _FakeConn([None, existing])
    with pytest.raises(WorkflowConflictError):
        put_record_in_transaction(
            conn, existing["run_id"], kind="dossier_version", record_key="main",
            version=1, payload={"a": 2}, digest="zzz",
        )


def test_put_record_rejects_bad_requests() -> None:
    conn = _FakeConn([])
    with pytest.raises(WorkflowError):
        put_record_in_transaction(
            conn, uuid4(), kind="bogus", record_key="k", version=1,
            payload={}, digest="d",
        )
    with pytest.raises(WorkflowError):
        put_record_in_transaction(
            conn, uuid4(), kind="scenario", record_key="", version=1,
            payload={}, digest="d",
        )
    with pytest.raises(WorkflowError):
        put_record_in_transaction(
            conn, uuid4(), kind="scenario", record_key="k", version=0,
            payload={}, digest="d",
        )


def test_get_record_latest_orders_by_version() -> None:
    conn = _FakeConn([_record_row(version=3)])
    record = get_record_in_transaction(conn, uuid4(), "scenario", "base")
    assert record is not None and record.version == 3
    assert "ORDER BY version DESC LIMIT 1" in conn._cursor.executed[0][0]


# --- Live PostgreSQL integration (GROWUP_PG_INTEGRATION=1) -------------------


def _require_workflow_tables() -> None:
    from app.properties_db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.workflow_runs') AS runs, "
                "to_regclass('public.workflow_records') AS records"
            )
            row = cur.fetchone()
    if row is None or row["runs"] is None or row["records"] is None:
        pytest.skip("workflow tables not present (migration 012 not applied)")


def _live_cleanup(run_ids: list[str]) -> None:
    from app.properties_db import get_connection

    ids = [str(item) for item in run_ids if item]
    if not ids:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM workflow_records WHERE run_id = ANY(%(ids)s)",
                {"ids": ids},
            )
            cur.execute(
                "DELETE FROM workflow_runs WHERE run_id = ANY(%(ids)s)",
                {"ids": ids},
            )


@needs_live_pg
def test_live_run_lifecycle() -> None:
    from app.properties_db import get_connection
    from app.workflow_db import (
        add_usage_in_transaction,
        append_error_in_transaction,
        get_run_in_transaction,
        list_records_in_transaction,
    )

    _require_workflow_tables()
    key = f"wf-test-{uuid4().hex}"
    created: list[str] = []
    try:
        with get_connection() as conn:
            run = create_run_in_transaction(
                conn, idempotency_key=key, correlation_id="corr-1",
                limits={"brave_attempts": 9},
            )
            created.append(str(run.run_id))
            replay = create_run_in_transaction(conn, idempotency_key=key)
            assert replay.run_id == run.run_id
            assert advance_stage_in_transaction(conn, run.run_id, "created", "gabriel") is True
            assert advance_stage_in_transaction(conn, run.run_id, "created", "raphael") is False
            add_usage_in_transaction(conn, run.run_id, "brave_attempts", 2)
            add_usage_in_transaction(conn, run.run_id, "brave_attempts", 1)
            append_error_in_transaction(conn, run.run_id, {"stage": "gabriel", "code": "x"})
            conn.commit()
        with get_connection() as conn:
            loaded = get_run_in_transaction(conn, run.run_id)
            assert loaded is not None
            assert loaded.stage == "gabriel"
            assert loaded.usage["brave_attempts"] == 3
            assert loaded.errors == [{"stage": "gabriel", "code": "x"}]
            assert loaded.limits == {"brave_attempts": 9}
            rec = put_record_in_transaction(
                conn, run.run_id, kind="qualification", record_key="main",
                version=1, payload={"outcome": "needs_research"}, digest="d1",
            )
            same = put_record_in_transaction(
                conn, run.run_id, kind="qualification", record_key="main",
                version=1, payload={"outcome": "needs_research"}, digest="d1",
            )
            assert same.record_id == rec.record_id
            with pytest.raises(WorkflowConflictError):
                put_record_in_transaction(
                    conn, run.run_id, kind="qualification", record_key="main",
                    version=1, payload={"outcome": "qualified"}, digest="d2",
                )
            v2 = put_record_in_transaction(
                conn, run.run_id, kind="qualification", record_key="main",
                version=2, payload={"outcome": "qualified"}, digest="d2",
            )
            assert v2.version == 2
            latest = get_record_in_transaction(conn, run.run_id, "qualification", "main")
            assert latest is not None and latest.version == 2
            first = get_record_in_transaction(conn, run.run_id, "qualification", "main", version=1)
            assert first is not None and first.payload == {"outcome": "needs_research"}
            assert len(list_records_in_transaction(conn, run.run_id)) == 2
            conn.commit()
    finally:
        _live_cleanup(created)


@needs_live_pg
def test_live_run_and_audit_commit_atomically() -> None:
    from app.observability.events import AgentEvent, RetentionClass, Severity
    from app.observability.policy import OperationClass
    from app.observability.transactions import run_consequential_transaction
    from app.properties_db import get_connection

    _require_workflow_tables()
    key = f"wf-audit-{uuid4().hex}"
    created: list[str] = []
    event_ids: list[str] = []
    try:
        event_id = f"evt-wf-{uuid4().hex}"
        event_ids.append(event_id)
        event = AgentEvent.model_validate(
            {
                "event_id": event_id,
                "agent": "gabriel",
                "run_id": key,
                "event_type": "gabriel.workflow.stage_completed",
                "severity": Severity.INFO.value,
                "summary": "synthetic workflow stage",
                "details": {"stage": "gabriel"},
                "evidence_refs": [],
                "retention_class": RetentionClass.PERMANENT.value,
            }
        )

        def _mutate(conn: Any) -> str:
            run = create_run_in_transaction(conn, idempotency_key=key)
            created.append(str(run.run_id))
            return str(run.run_id)

        run_id = run_consequential_transaction(
            operation_class=OperationClass.CONSEQUENTIAL_INTERNAL,
            event=event,
            mutate=_mutate,
        )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM agent_events WHERE event_id = %(eid)s",
                    {"eid": event_id},
                )
                assert cur.fetchone()["n"] == 1
        assert run_id == created[0]
    finally:
        _live_cleanup(created)
        if event_ids:
            from app.properties_db import get_connection as _get_conn

            with _get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM agent_events WHERE event_id = ANY(%(ids)s)",
                        {"ids": event_ids},
                    )

@needs_live_pg
def test_live_identity_review_record_round_trip() -> None:
    """The identity_review kind persists in real workflow_records rows."""
    from app.properties_db import get_connection
    from app.workflow import KIND_IDENTITY_REVIEW, digest_payload
    from app.workflow_db import (
        create_run_in_transaction,
        get_record_in_transaction,
        put_record_in_transaction,
    )

    _require_workflow_tables()
    key = f"wf-review-{uuid4().hex}"
    created: list[str] = []
    try:
        with get_connection() as conn:
            run = create_run_in_transaction(conn, idempotency_key=key)
            created.append(str(run.run_id))
            payload = {
                "reason": "multiple_dossiers_match_target",
                "property_id": str(uuid4()),
                "dossier_keys": ["a1", "a2"],
                "candidate_ids": [],
                "assignment_id": "live-a1",
                "status": "pending",
            }
            stored = put_record_in_transaction(
                conn, run.run_id, kind=KIND_IDENTITY_REVIEW,
                record_key="dossier-association", version=1,
                payload=payload, digest=digest_payload(payload),
            )
            reread = get_record_in_transaction(
                conn, run.run_id, KIND_IDENTITY_REVIEW,
                "dossier-association",
            )
            assert reread is not None
            assert reread.payload["reason"] == "multiple_dossiers_match_target"
            assert reread.digest == stored.digest
    finally:
        _live_cleanup(created)
