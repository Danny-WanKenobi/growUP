"""Real-path orchestration tests: resume, gating, fail-closed stops."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app import mvp as mvp_module
from app.workflow_db import WorkflowError


class _Conn:
    pass


def _run(stage: str, status: str = "open", errors: Any = None) -> SimpleNamespace:
    return SimpleNamespace(run_id=uuid4(), stage=stage, status=status, errors=errors or [])


@pytest.fixture()
def _enabled(monkeypatch) -> None:
    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")


def _stub_gabriel(monkeypatch, calls: dict[str, int]) -> None:
    def _acquire(conn: Any, run_id: Any, aid: str, query: str, **kwargs: Any) -> dict[str, Any]:
        calls["acquire"] = calls.get("acquire", 0) + 1
        return {"attempt": "completed", "assignment_id": aid, "query": query, "results": []}

    def _replay(conn: Any, run_id: Any, aid: str) -> dict[str, Any]:
        return {"attempt": "completed", "assignment_id": aid, "query": "q", "results": []}

    monkeypatch.setattr(mvp_module, "acquire_evidence_in_transaction", _acquire)
    monkeypatch.setattr(mvp_module, "replay_artifact", _replay)
    monkeypatch.setattr(mvp_module, "begin_attempt", lambda *a, **k: calls.update({"begun": 1}))
    monkeypatch.setattr(mvp_module, "get_record_in_transaction", lambda *a, **k: None)
    from app.agents.gabriel.extractor import ResearchIntelligence

    monkeypatch.setattr(
        mvp_module, "extract_research_intelligence", lambda results: ResearchIntelligence(results=[])
    )
    monkeypatch.setattr(mvp_module, "rank_intelligence", lambda intel: SimpleNamespace(candidates=[]))
    monkeypatch.setattr(
        mvp_module, "build_dossiers",
        lambda req: SimpleNamespace(dossiers=[], dossier_version="t"),
    )
    monkeypatch.setattr(mvp_module, "persist_research_artifacts", lambda *a: ["a1"])
    monkeypatch.setattr(
        mvp_module, "persist_dossier_versions", lambda *a, **k: ["d"]
    )
    monkeypatch.setattr(mvp_module, "persist_qualifications", lambda *a: [])


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MVP_WORKFLOW_ENABLED", raising=False)
    with pytest.raises(WorkflowError, match="disabled"):
        mvp_module.evaluate_real(_Conn(), uuid4(), "a1", "q")


def test_stopped_run_refuses(monkeypatch, _enabled: None) -> None:
    monkeypatch.setattr(mvp_module, "get_run_in_transaction", lambda c, r: _run("gabriel", "stopped"))
    with pytest.raises(WorkflowError, match="stopped"):
        mvp_module.evaluate_real(_Conn(), uuid4(), "a1", "q")


def test_fail_closed_stop_skips_scenarios(monkeypatch, _enabled: None) -> None:
    calls: dict[str, int] = {}
    stages = {"stage": "created"}
    monkeypatch.setattr(
        mvp_module, "get_run_in_transaction", lambda c, r: _run(stages["stage"])
    )

    def _advance(conn: Any, rid: Any, expected: str, nxt: str, status: str = "open") -> bool:
        stages["stage"] = nxt
        calls["advance"] = calls.get("advance", 0) + 1
        return True

    monkeypatch.setattr(mvp_module, "advance_stage_in_transaction", _advance)
    _stub_gabriel(monkeypatch, calls)
    authorized = {"outcome": "needs_evidence", "basis": ["single_voice_only"], "dossier_digest": "D"}
    monkeypatch.setattr(
        mvp_module, "authorize_in_transaction", lambda c, r, k, v=None: authorized
    )
    dossier = SimpleNamespace(
        payload={"property_id": str(uuid4()), "dossier": {}}, digest="D"
    )

    def _get_record(conn: Any, rid: Any, kind: str, key: Any = None, version: Any = None) -> Any:
        if kind == "dossier_version":
            return dossier
        return None

    monkeypatch.setattr(mvp_module, "get_record_in_transaction", _get_record)
    # No dossier versions persisted by stubs; authorize needs a target key.
    monkeypatch.setattr(
        mvp_module, "list_records_in_transaction",
        lambda conn, rid, kind=None: [SimpleNamespace(record_key="k1")],
    )
    monkeypatch.setattr(
        mvp_module, "put_record_in_transaction",
        lambda conn, rid, **k: SimpleNamespace(payload=k["payload"], version=k["version"]),
    )
    monkeypatch.setattr(
        mvp_module, "create_version_in_transaction",
        lambda conn, **k: SimpleNamespace(dossier_id=uuid4(), version=1),
    )
    monkeypatch.setattr(
        mvp_module, "snapshot_in_transaction",
        lambda conn, **k: SimpleNamespace(snapshot_id=uuid4()),
    )
    monkeypatch.setattr(
        mvp_module, "issue_receipt_in_transaction",
        lambda conn, **k: SimpleNamespace(receipt_id=uuid4()),
    )

    def _scenarios(*a: Any, **k: Any) -> Any:
        raise AssertionError("scenarios must not run without authorization")

    monkeypatch.setattr(mvp_module, "run_scenario_in_transaction", _scenarios)
    summary = mvp_module.evaluate_real(_Conn(), uuid4(), "a1", "q", approved=True)
    assert summary["decision"] == "needs_evidence"
    assert summary["stage"] == "done"
    assert calls.get("acquire") == 1


def test_authorized_path_runs_both_scenarios(monkeypatch, _enabled: None) -> None:
    calls: dict[str, int] = {}
    stages = {"stage": "raphael"}
    monkeypatch.setattr(
        mvp_module, "get_run_in_transaction", lambda c, r: _run(stages["stage"])
    )

    def _advance(conn: Any, rid: Any, expected: str, nxt: str, status: str = "open") -> bool:
        stages["stage"] = nxt
        return True

    monkeypatch.setattr(mvp_module, "advance_stage_in_transaction", _advance)
    _stub_gabriel(monkeypatch, calls)  # acquire must NOT be called on resume

    def _scenario(conn: Any, rid: Any, **kwargs: Any) -> dict[str, Any]:
        calls[kwargs["config_name"]] = 1
        return {"complete": True, "missing": [], "outputs": {}}

    monkeypatch.setattr(mvp_module, "run_scenario_in_transaction", _scenario)
    monkeypatch.setattr(
        mvp_module, "list_records_in_transaction",
        lambda conn, rid, kind=None: [SimpleNamespace(record_key="d1")],
    )
    link_payload = {
        "dossier_id": str(uuid4()),
        "custody_version": 1,
        "property_id": str(uuid4()),
        "dossier_key": "main",
        "receipts": {},
    }

    def _get_record(conn: Any, rid: Any, kind: str, key: Any = None, version: Any = None) -> Any:
        if kind == "custody_link":
            return SimpleNamespace(payload=dict(link_payload), version=2)
        if kind == "scenario":
            return SimpleNamespace(
                payload={
                    "complete": True, "missing": [], "outputs": {"sites": 1},
                    "financial_assessment": {
                        "assessment": {"complete": True, "verdict": "favorable_under_assumptions"}
                    },
                },
                version=1,
            )
        if kind == "dossier_version":
            return SimpleNamespace(
                payload={"property_id": link_payload["property_id"], "dossier": {}},
                digest="D",
            )
        return None

    monkeypatch.setattr(mvp_module, "get_record_in_transaction", _get_record)
    monkeypatch.setattr(
        mvp_module, "put_record_in_transaction",
        lambda conn, rid, **k: SimpleNamespace(payload=k["payload"], version=k["version"]),
    )
    monkeypatch.setattr(
        mvp_module, "issue_receipt_in_transaction",
        lambda conn, **k: SimpleNamespace(receipt_id=uuid4()),
    )
    monkeypatch.setattr(
        mvp_module, "prepare_package_in_transaction",
        lambda conn, **k: {"entry_id": str(uuid4()), "package_state": "draft"},
    )
    summary = mvp_module.evaluate_real(
        _Conn(), uuid4(), "a1", "q", site_inputs={}, approved=True
    )
    assert summary["stage"] == "done"
    assert set(summary["scenarios"]) == {"bench", "rack3"}
    assert summary["package"]["package_state"] == "draft"
    assert summary["simulation"] == "not requested"
    assert "acquire" not in calls  # resume did not re-acquire


def test_ambiguous_prior_attempt_refuses_retry(monkeypatch, _enabled: None) -> None:
    calls: dict[str, int] = {}
    marker = [{"assignment_id": "a1", "attempt": "started"}]
    monkeypatch.setattr(
        mvp_module, "get_run_in_transaction", lambda c, r: _run("created", errors=marker)
    )
    _stub_gabriel(monkeypatch, calls)
    with pytest.raises(WorkflowError, match="ambiguous prior attempt"):
        mvp_module.evaluate_real(_Conn(), uuid4(), "a1", "q", approved=True)
    assert "acquire" not in calls


def test_recorded_failed_attempt_refuses_retry(monkeypatch, _enabled: None) -> None:
    calls: dict[str, int] = {}
    monkeypatch.setattr(
        mvp_module, "get_run_in_transaction", lambda c, r: _run("created")
    )
    _stub_gabriel(monkeypatch, calls)
    failed = SimpleNamespace(payload={"attempt": "ambiguous", "error": "timeout"})
    monkeypatch.setattr(mvp_module, "get_record_in_transaction", lambda *a, **k: failed)
    with pytest.raises(WorkflowError, match="prior acquisition recorded"):
        mvp_module.evaluate_real(_Conn(), uuid4(), "a1", "q", approved=True)
    assert "acquire" not in calls
    assert "begun" not in calls


def test_stop_run_sets_status(monkeypatch, _enabled: None) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(mvp_module, "get_run_in_transaction", lambda c, r: _run("gabriel"))
    monkeypatch.setattr(
        mvp_module, "set_status_in_transaction",
        lambda c, r, s: seen.update({"status": s}),
    )
    mvp_module.stop_run(_Conn(), uuid4())
    assert seen == {"status": "stopped"}


def test_audited_start_binds_permanent_event(monkeypatch, _enabled: None) -> None:
    seen: dict[str, Any] = {}

    def _coordinate(**kwargs: Any) -> Any:
        seen.update(kwargs)
        assert kwargs["event"].retention_class.value == "permanent"
        assert kwargs["event"].event_type == "wondrue.evaluation.completed"
        return kwargs["mutate"](_Conn())

    monkeypatch.setattr(
        "app.observability.transactions.run_consequential_transaction", _coordinate
    )
    monkeypatch.setattr(
        mvp_module, "evaluate_real", lambda *a, **k: {"stage": "done"}
    )
    import app.workflow_db as workflow_db

    monkeypatch.setattr(
        workflow_db, "create_run_in_transaction",
        lambda conn, **k: SimpleNamespace(run_id=uuid4()),
    )
    summary = mvp_module.start_evaluation_audited("a1", "q", approved=True)
    assert summary == {"stage": "done"}
    assert seen["operation_class"].value == "consequential_internal"


def test_audited_stop_binds_permanent_event(monkeypatch, _enabled: None) -> None:
    seen: dict[str, Any] = {}

    def _coordinate(**kwargs: Any) -> Any:
        seen.update(kwargs)
        assert kwargs["event"].event_type == "wondrue.evaluation.stopped"
        return kwargs["mutate"](_Conn())

    monkeypatch.setattr(
        "app.observability.transactions.run_consequential_transaction", _coordinate
    )
    monkeypatch.setattr(mvp_module, "stop_run", lambda *a: seen.update({"stopped": True}))
    mvp_module.stop_run_audited(uuid4())
    assert seen.get("stopped") is True


def test_record_model_execution_persists(monkeypatch) -> None:
    from app.model_adapter import ModelResult

    seen: dict[str, Any] = {}

    def _put(conn: Any, rid: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(payload=kwargs["payload"])

    monkeypatch.setattr(mvp_module, "put_record_in_transaction", _put)
    result = ModelResult(
        provider="meta", model="m", mode="replay", purpose="p", result={"a": 1}
    )
    stored = mvp_module.record_model_execution(_Conn(), uuid4(), "m1", result)
    assert stored["mode"] == "replay"
    assert seen["kind"] == "model_execution"


def _dossier_ns(key: str, street: str = "", city: str = "",
              state: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key, street_address=street, city=city, state=state
    )


def _stub_association(monkeypatch, dossiers: list[SimpleNamespace],
                      target_row: dict[str, Any] | None,
                      stored_ids: list[str],
                      seen: dict[str, Any]) -> None:
    calls: dict[str, int] = {}
    _stub_gabriel(monkeypatch, calls)
    monkeypatch.setattr(mvp_module, "get_run_in_transaction", lambda c, r: None)
    monkeypatch.setattr(
        mvp_module, "build_dossiers",
        lambda req: SimpleNamespace(dossiers=dossiers, dossier_version="t"),
    )
    monkeypatch.setattr(
        mvp_module, "persist_dossier_versions",
        lambda *a, **k: seen.setdefault("property_ids", k.get("property_ids")),
    )
    monkeypatch.setattr(
        "app.properties_db.get_property", lambda pid, config=None: target_row
    )
    monkeypatch.setattr(
        "app.properties_db.find_by_address",
        lambda **k: [{"id": pid} for pid in stored_ids],
    )
    monkeypatch.setattr(
        mvp_module, "put_record_in_transaction",
        lambda *a, **k: seen.setdefault("reviews", []).append(k),
    )


def test_gabriel_stage_associates_only_matching_dossier(
    monkeypatch, _enabled: None
) -> None:
    """Search for A returning A/B/C associates only A with A."""
    pid = str(uuid4())
    seen: dict[str, Any] = {}
    _stub_association(
        monkeypatch,
        [
            _dossier_ns("a", "500 Commerce St", "Dallas", "TX"),
            _dossier_ns("b", "600 Main St", "Dallas", "TX"),
            _dossier_ns("c"),  # addressless: preserved, never associated
        ],
        {"id": pid, "street_address": "500 Commerce St", "city": "Dallas",
         "state": "TX", "zip_code": "75202"},
        [pid],
        seen,
    )
    keys, _outcomes, review = mvp_module._gabriel_stage(
        _Conn(), uuid4(), "a1", "q", True, 1.0, property_id=pid
    )
    assert keys == ["a", "b", "c"]
    assert seen["property_ids"] == {"a": pid}  # B/C preserved, unassociated
    assert review is None
    assert "reviews" not in seen


def test_gabriel_stage_absent_match_forces_no_association(
    monkeypatch, _enabled: None
) -> None:
    pid = str(uuid4())
    seen: dict[str, Any] = {}
    _stub_association(
        monkeypatch,
        [_dossier_ns("b", "600 Main St", "Dallas", "TX")],
        {"id": pid, "street_address": "500 Commerce St", "city": "Dallas",
         "state": "TX", "zip_code": "75202"},
        [pid],
        seen,
    )
    _keys, _outcomes, review = mvp_module._gabriel_stage(
        _Conn(), uuid4(), "a1", "q", True, 1.0, property_id=pid
    )
    assert seen["property_ids"] == {}
    assert review is None  # absent is not ambiguous: no review, no guess


def test_gabriel_stage_missing_target_row_forces_no_association(
    monkeypatch, _enabled: None
) -> None:
    pid = str(uuid4())
    seen: dict[str, Any] = {}
    _stub_association(
        monkeypatch,
        [_dossier_ns("a", "500 Commerce St", "Dallas", "TX")],
        None, [pid], seen,
    )
    _keys, _outcomes, review = mvp_module._gabriel_stage(
        _Conn(), uuid4(), "a1", "q", True, 1.0, property_id=pid
    )
    assert seen["property_ids"] == {}
    assert review is None


def test_gabriel_stage_multiple_matches_route_to_review(
    monkeypatch, _enabled: None
) -> None:
    pid = str(uuid4())
    seen: dict[str, Any] = {}
    _stub_association(
        monkeypatch,
        [
            _dossier_ns("a1", "500 Commerce St", "Dallas", "TX"),
            _dossier_ns("a2", "500 Commerce St.", "dallas", "tx"),
        ],
        {"id": pid, "street_address": "500 Commerce St", "city": "Dallas",
         "state": "TX", "zip_code": "75202"},
        [pid],
        seen,
    )
    _keys, _outcomes, review = mvp_module._gabriel_stage(
        _Conn(), uuid4(), "a1", "q", True, 1.0, property_id=pid
    )
    assert seen["property_ids"] == {}
    assert review is not None
    assert review["reason"] == "multiple_dossiers_match_target"
    assert review["dossier_keys"] == ["a1", "a2"]
    assert len(seen["reviews"]) == 1
    assert seen["reviews"][0]["kind"] == "identity_review"
    assert seen["reviews"][0]["payload"]["status"] == "pending"


def test_gabriel_stage_shared_target_address_routes_to_review(
    monkeypatch, _enabled: None
) -> None:
    pid, other = str(uuid4()), str(uuid4())
    seen: dict[str, Any] = {}
    _stub_association(
        monkeypatch,
        [_dossier_ns("a", "500 Commerce St", "Dallas", "TX")],
        {"id": pid, "street_address": "500 Commerce St", "city": "Dallas",
         "state": "TX", "zip_code": "75202"},
        [pid, other],  # two stored buildings share the address
        seen,
    )
    _keys, _outcomes, review = mvp_module._gabriel_stage(
        _Conn(), uuid4(), "a1", "q", True, 1.0, property_id=pid
    )
    assert seen["property_ids"] == {}
    assert review is not None
    assert review["reason"] == "target_address_shared_by_multiple_properties"
    assert review["candidate_ids"] == sorted([pid, other])


def test_select_custody_target_prefers_associated(monkeypatch) -> None:
    pid = str(uuid4())
    records = [
        SimpleNamespace(record_key="main", payload={"property_id": ""}),
        SimpleNamespace(record_key="alt", payload={"property_id": pid}),
    ]
    monkeypatch.setattr(
        mvp_module, "list_records_in_transaction", lambda c, r, k: records
    )
    monkeypatch.setattr(
        mvp_module, "get_record_in_transaction", lambda c, r, k, key: records[0]
    )
    assert mvp_module._select_custody_target(_Conn(), uuid4(), None) == "alt"
    assert mvp_module._select_custody_target(_Conn(), uuid4(), "main") == "main"
    records[1].payload["property_id"] = ""
    assert mvp_module._select_custody_target(_Conn(), uuid4(), None) == "main"


def test_custody_block_references_pending_identity_review(
    monkeypatch, _enabled: None
) -> None:
    stages = {"stage": "gabriel"}
    monkeypatch.setattr(
        mvp_module, "get_run_in_transaction", lambda c, r: _run(stages["stage"])
    )

    def _advance(conn: Any, rid: Any, expected: str, nxt: str, status: str = "open") -> bool:
        stages["stage"] = nxt
        return True

    monkeypatch.setattr(mvp_module, "advance_stage_in_transaction", _advance)
    unassociated = SimpleNamespace(payload={"property_id": "", "dossier": {}}, digest="D")
    review = SimpleNamespace(payload={"reason": "association ambiguous"}, version=1)

    def _get_record(conn: Any, rid: Any, kind: str, key: Any = None, version: Any = None) -> Any:
        if kind == "dossier_version":
            return unassociated
        if kind == "identity_review":
            return review
        return None

    monkeypatch.setattr(mvp_module, "get_record_in_transaction", _get_record)
    monkeypatch.setattr(
        mvp_module, "list_records_in_transaction",
        lambda conn, rid, kind=None: [SimpleNamespace(record_key="k1")],
    )

    def _authorize(*a: Any, **k: Any) -> Any:
        raise AssertionError("custody must not authorize without property identity")

    monkeypatch.setattr(mvp_module, "authorize_in_transaction", _authorize)
    summary = mvp_module.evaluate_real(_Conn(), uuid4(), "a1", "q", approved=True)
    assert summary["stage"] == "done"
    assert "no resolved property identity" in summary["custody"]
    assert "identity review pending" in summary["custody"]


def test_gabriel_stage_rejects_demo_and_invalid_property_ids(
    monkeypatch, _enabled: None
) -> None:
    calls: dict[str, int] = {}
    _stub_gabriel(monkeypatch, calls)
    monkeypatch.setattr(mvp_module, "get_run_in_transaction", lambda c, r: None)
    with pytest.raises(WorkflowError, match="demo"):
        mvp_module._gabriel_stage(
            _Conn(), uuid4(), "a1", "q", True, 1.0, property_id="demo-x"
        )
    with pytest.raises(WorkflowError, match="operational UUID"):
        mvp_module._gabriel_stage(
            _Conn(), uuid4(), "a1", "q", True, 1.0, property_id="not-a-uuid"
        )
    assert "acquire" not in calls  # validation precedes any provider work
