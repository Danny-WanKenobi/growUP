"""Raphael authorization and acquisition tests (no network, no PG)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.agents.raphael import acquire as raphael_acquire
from app.agents.raphael.service import authorize_in_transaction, evaluate_dossier
from app.agents.gabriel.sources.brave import BraveTimeout
from app.pilot import canonical_quota_id
from app.workflow import digest_payload
from app.workflow_db import WorkflowError


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
        if not self._script:
            return None
        item = self._script.pop(0)
        if item == "ECHO_INSERT":
            from uuid import uuid4 as _uuid4
            import json as _json

            params = self.executed[-1][1]
            payload = params.get("payload")
            if isinstance(payload, str):
                payload = _json.loads(payload)
            return {
                "record_id": _uuid4(),
                "run_id": params.get("run"),
                "kind": params.get("kind"),
                "record_key": params.get("key"),
                "version": params.get("version", 1),
                "payload": payload,
                "digest": params.get("digest"),
            }
        return item

    def fetchall(self) -> Any:
        return self._script.pop(0) if self._script else []


class _FakeConn:
    def __init__(self, script: list[Any]) -> None:
        self._cursor = _FakeCursor(script)

    def cursor(self) -> _FakeCursor:
        return self._cursor


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "builder_version": "t",
        "artifact_refs": ["a1"],
        "dossier": {
            "identity_disputes": {},
            "conflicts": [],
            "category_attribution": {"status": "observed"},
            "sources": ["https://broker-a.example/x"],
        },
        "candidate_verification": {
            "outcome": "VERIFIED",
            "pathway": "CREDIBLE_CORROBORATION",
            "reasons": [],
        },
        "property_id": "",
    }
    payload.update(overrides)
    payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
    return payload


def _dossier_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": uuid4(),
        "run_id": uuid4(),
        "kind": "dossier_version",
        "record_key": "main",
        "version": 1,
        "payload": payload,
        "digest": payload["digest"],
    }


def test_evaluate_authorized_when_all_gates_pass() -> None:
    verdict = evaluate_dossier(_payload())
    assert verdict["outcome"] == "authorized"


def test_evaluate_needs_evidence_for_failed_verification() -> None:
    payload = _payload(candidate_verification={"outcome": "NOT_VERIFIED", "reasons": ["single_voice_only"]})
    verdict = evaluate_dossier(payload)
    assert verdict["outcome"] == "needs_evidence"
    assert "single_voice_only" in verdict["basis"][0]


def test_evaluate_needs_evidence_for_missing_verification() -> None:
    payload = _payload()
    del payload["candidate_verification"]
    payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
    verdict = evaluate_dossier(payload)
    assert verdict["outcome"] == "needs_evidence"


def test_evaluate_needs_evidence_for_disputes_and_conflicts() -> None:
    payload = _payload()
    payload["dossier"]["identity_disputes"] = {"name": ["A", "B"]}
    payload["dossier"]["conflicts"] = [{"topic": "asking_price"}]
    payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
    verdict = evaluate_dossier(payload)
    assert verdict["outcome"] == "needs_evidence"
    assert len(verdict["basis"]) == 2


def test_evaluate_rejects_contradictory_category() -> None:
    payload = _payload()
    payload["dossier"]["category_attribution"] = {
        "status": "contradictory",
        "conflicting_values": ["commercial_property", "warehouse_industrial"],
    }
    payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
    verdict = evaluate_dossier(payload)
    assert verdict["outcome"] == "rejected"
    assert "contradicts" in verdict["basis"][-1]


def _decision_row(payload: dict[str, Any], digest: str) -> dict[str, Any]:
    return {
        "record_id": uuid4(),
        "run_id": uuid4(),
        "kind": "raphael_decision",
        "record_key": "k",
        "version": 1,
        "payload": payload,
        "digest": digest,
    }


def test_authorize_persists_decision_with_scope() -> None:
    payload = _payload()
    conn = _FakeConn([_dossier_row(payload), "ECHO_INSERT"])
    stored = authorize_in_transaction(conn, uuid4(), "main")
    assert stored["outcome"] == "authorized"
    assert stored["dossier_digest"] == payload["digest"]
    assert stored["policy_version"] == "mvp-raphael-1"
    assert "zoning" in " ".join(stored["limitations"]).lower()
    assert "structural" in " ".join(stored["limitations"]).lower()


def test_authorize_records_assessed_and_authorized_for_known_cell() -> None:
    payload = _payload()
    payload["property_id"] = str(uuid4())
    payload["dossier"]["county_attribution"] = {
        "evidence_status": "verified", "value": "Dallas County",
    }
    payload["dossier"]["category_attribution"] = {
        "status": "observed", "observed": "high_rise",
    }
    payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
    conn = _FakeConn([
        _dossier_row(payload), "ECHO_INSERT",
        {"county": "dallas county"}, {"event_id": uuid4()},
        {"county": "dallas county"}, {"event_id": uuid4()},
    ])
    stored = authorize_in_transaction(conn, uuid4(), "main")
    assert stored["outcome"] == "authorized"
    transitions = [
        params["transition"] for sql, params in conn._cursor.executed
        if "INSERT INTO pilot_events" in sql
    ]
    assert transitions == ["assessed", "authorized"]
    dossiers = {
        str(params["dossier"])
        for sql, params in conn._cursor.executed
        if "INSERT INTO pilot_events" in sql
    }
    assert dossiers == {str(canonical_quota_id(payload["property_id"]))}


def test_authorize_skips_pilot_count_without_resolved_property() -> None:
    payload = _payload()
    payload["dossier"]["county_attribution"] = {
        "evidence_status": "verified", "value": "Dallas County",
    }
    payload["dossier"]["category_attribution"] = {
        "status": "observed", "observed": "high_rise",
    }
    payload["digest"] = digest_payload({k: v for k, v in payload.items() if k != "digest"})
    conn = _FakeConn([_dossier_row(payload), "ECHO_INSERT"])
    stored = authorize_in_transaction(conn, uuid4(), "main")
    assert stored["outcome"] == "authorized"  # decision persists; only counting skips
    assert not [
        sql for sql, _ in conn._cursor.executed if "INSERT INTO pilot_events" in sql
    ]


def test_authorize_rejects_digest_mismatch() -> None:
    payload = _payload()
    row = _dossier_row(payload)
    row["digest"] = "tampered"
    conn = _FakeConn([row])
    with pytest.raises(WorkflowError, match="digest binding"):
        authorize_in_transaction(conn, uuid4(), "main")


def test_authorize_requires_dossier() -> None:
    conn = _FakeConn([None])
    with pytest.raises(WorkflowError, match="no dossier version"):
        authorize_in_transaction(conn, uuid4(), "missing")


def test_authorize_is_idempotent_per_digest() -> None:
    from app.agents.raphael.service import LIMITATIONS, SCOPE

    payload = _payload(
        candidate_verification={"outcome": "NOT_VERIFIED", "reasons": ["single_voice_only"]}
    )
    verdict = evaluate_dossier(payload)
    assert verdict["outcome"] == "needs_evidence"
    decision = {
        "outcome": verdict["outcome"],
        "dossier_key": "main",
        "dossier_digest": payload["digest"],
        "dossier_version": 1,
        "basis": verdict["basis"],
        "policy_version": "mvp-raphael-1",
        "scope": SCOPE,
        "limitations": list(LIMITATIONS),
    }
    digest = digest_payload(decision)
    conn = _FakeConn([_dossier_row(payload), None, _decision_row(decision, digest)])
    stored = authorize_in_transaction(conn, uuid4(), "main")
    assert stored["outcome"] == "needs_evidence"
    assert stored["dossier_digest"] == payload["digest"]


def _run_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "run_id": uuid4(),
        "idempotency_key": "k",
        "stage": "raphael",
        "status": "open",
        "correlation_id": "",
        "limits": {"brave_attempts": 9},
        "usage": {},
        "errors": [],
    }
    row.update(overrides)
    return row


def _artifact_row(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": uuid4(),
        "run_id": uuid4(),
        "kind": "research_artifact",
        "record_key": "a1",
        "version": 1,
        "payload": payload,
        "digest": digest_payload(payload),
    }


def test_acquire_requires_approval() -> None:
    conn = _FakeConn([_run_row()])
    with pytest.raises(WorkflowError, match="explicit approval"):
        raphael_acquire.acquire_evidence_in_transaction(
            conn, uuid4(), "a1", "q", approved=False
        )


def test_begin_attempt_records_charge_and_marker() -> None:
    conn = _FakeConn([_run_row()])
    raphael_acquire.begin_attempt_in_transaction(conn, uuid4(), "a1", approved=True)
    statements = [sql for sql, _ in conn._cursor.executed]
    assert any("jsonb_set(usage" in sql for sql in statements)
    assert any("errors = errors ||" in sql for sql in statements)


def test_begin_attempt_requires_approval_and_bound() -> None:
    conn = _FakeConn([_run_row()])
    with pytest.raises(WorkflowError, match="explicit approval"):
        raphael_acquire.begin_attempt_in_transaction(conn, uuid4(), "a1", approved=False)
    conn = _FakeConn([_run_row(usage={"brave_attempts": 9})])
    with pytest.raises(WorkflowError, match="bound reached"):
        raphael_acquire.begin_attempt_in_transaction(conn, uuid4(), "a1", approved=True)


def test_acquire_enforces_run_bound() -> None:
    conn = _FakeConn([_run_row(usage={"brave_attempts": 10})])
    with pytest.raises(WorkflowError, match="bound reached"):
        raphael_acquire.acquire_evidence_in_transaction(
            conn, uuid4(), "a1", "q", approved=True
        )


def test_acquire_caps_limit_at_eighteen() -> None:
    conn = _FakeConn([_run_row(usage={"brave_attempts": 19}, limits={"brave_attempts": 99})])
    with pytest.raises(WorkflowError, match="bound reached"):
        raphael_acquire.acquire_evidence_in_transaction(
            conn, uuid4(), "a1", "q", approved=True
        )


def test_acquire_persists_completed_artifact(monkeypatch) -> None:
    item = SimpleNamespace(
        model_dump=lambda mode=None: {
            "url": "https://broker-a.example/x", "title": "T", "snippet": "S",
            "domain": "broker-a.example", "published_at": None,
            "retrieved_at": "2026-10-30T00:00:00+00:00", "rank": 1,
        }
    )
    monkeypatch.setattr(raphael_acquire, "execute_query", lambda q, aid, t: [item])
    conn = _FakeConn([_run_row(), "ECHO_INSERT"])
    stored = raphael_acquire.acquire_evidence_in_transaction(
        conn, uuid4(), "a1", "q", approved=True
    )
    assert stored["attempt"] == "completed"
    assert stored["collection_provenance"] == "adapter_collected"
    # Usage was charged by begin_attempt, not here.
    assert not any("jsonb_set(usage" in sql for sql, _ in conn._cursor.executed)


def test_acquire_records_ambiguous_timeout(monkeypatch) -> None:
    def _boom(query: str, assignment_id: str, timeout: float) -> Any:
        raise BraveTimeout("slow")

    monkeypatch.setattr(raphael_acquire, "execute_query", _boom)
    conn = _FakeConn([_run_row(), "ECHO_INSERT"])
    stored = raphael_acquire.acquire_evidence_in_transaction(
        conn, uuid4(), "a1", "q", approved=True
    )
    assert stored["attempt"] == "ambiguous"
    assert "timeout" in stored["error"]


def test_replay_loads_without_network() -> None:
    payload = {"provider": "brave", "attempt": "completed"}
    conn = _FakeConn([_artifact_row(payload)])
    assert raphael_acquire.replay_artifact(conn, uuid4(), "a1") == payload


def test_replay_missing_raises() -> None:
    conn = _FakeConn([None])
    with pytest.raises(WorkflowError, match="no persisted artifact"):
        raphael_acquire.replay_artifact(conn, uuid4(), "nope")


# --- Live PostgreSQL integration (GROWUP_PG_INTEGRATION=1) -------------------

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


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
def test_live_authorize_is_idempotent_per_digest() -> None:
    from app.properties_db import get_connection
    from app.workflow_db import (
        create_run_in_transaction,
        get_record_in_transaction,
        put_record_in_transaction,
    )

    _require_workflow_tables()
    created: list[str] = []
    try:
        with get_connection() as conn:
            run = create_run_in_transaction(
                conn, idempotency_key=f"raphael-test-{uuid4().hex}"
            )
            created.append(str(run.run_id))
            payload = _payload(
                candidate_verification={
                    "outcome": "NOT_VERIFIED",
                    "reasons": ["single_voice_only"],
                }
            )
            put_record_in_transaction(
                conn, run.run_id, kind="dossier_version", record_key="main",
                version=1, payload=payload, digest=str(payload["digest"]),
            )
            first = authorize_in_transaction(conn, run.run_id, "main")
            second = authorize_in_transaction(conn, run.run_id, "main")
            assert first == second
            assert first["outcome"] == "needs_evidence"
            decision = get_record_in_transaction(
                conn, run.run_id, "raphael_decision", str(payload["digest"])
            )
            assert decision is not None
            assert decision.payload["dossier_digest"] == payload["digest"]
            conn.commit()
    finally:
        _live_cleanup(created)
