"""Wondrue modes, Meta adapter, and routes (no live calls, no PG)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.demo import DEMO_LABEL, DemoStore
from app.main import app
from app.model_adapter import (
    MetaLiveAdapter,
    ModelError,
    ModelRequest,
    ReplayAdapter,
    parse_model_json,
    validate_config_proposal,
    validate_intent,
)
from app.wondrue import (
    APPROVALS,
    DEMO,
    WondrueAuthError,
    WondrueError,
    compare_with_proposal,
    handle_command,
)

client = TestClient(app, raise_server_exceptions=False)

_OPERATOR_HEADERS = {"X-Wondrue-Key": "operator-secret"}
_READER_HEADERS = {"X-Wondrue-Key": "reader-secret"}


@pytest.fixture(autouse=True)
def _clean_demo(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("WONDRUE_OPERATOR_KEY", "operator-secret")
    monkeypatch.setenv("WONDRUE_READER_KEY", "reader-secret")
    monkeypatch.delenv("EMERGENCY_DIAGNOSTIC_DEMO", raising=False)
    DEMO.reset()
    APPROVALS._pending.clear()
    yield
    DEMO.reset()
    APPROVALS._pending.clear()


def test_validate_intent_allowlists() -> None:
    assert validate_intent({"intent": "help"}) == {"intent": "help", "args": {}}
    with pytest.raises(ModelError, match="unknown intent"):
        validate_intent({"intent": "drop_tables"})
    with pytest.raises(ModelError, match="must be a JSON object"):
        validate_intent("help")
    with pytest.raises(ModelError, match="unsupported type"):
        validate_intent({"intent": "help", "args": {"x": {"nested": True}}})
    with pytest.raises(ModelError, match="too long"):
        validate_intent({"intent": "help", "args": {"x": "y" * 2001}})


def test_validate_proposal_enforces_catalog() -> None:
    cleaned = validate_config_proposal(
        {"options": [{"config": "rack3", "overrides": {"price_per_kg": 9}}], "note": "n"}
    )
    assert cleaned["options"][0]["config"] == "rack3"
    with pytest.raises(ModelError, match="catalog"):
        validate_config_proposal({"options": [{"config": "aeroponic"}], "note": ""})
    with pytest.raises(ModelError, match="1-4 options"):
        validate_config_proposal({"options": [], "note": ""})
    with pytest.raises(ModelError, match="must be a JSON object"):
        validate_config_proposal("bench")


def test_parse_model_json_rejects_malformed() -> None:
    assert parse_model_json('{"a": 1}') == {"a": 1}
    with pytest.raises(ModelError, match="not valid JSON"):
        parse_model_json("Sure thing! Here is JSON-ish text")


def test_replay_adapter_labels_replays() -> None:
    adapter = ReplayAdapter({"p": {"intent": "help", "args": {}}})
    result = adapter.complete(ModelRequest("p", {}))
    assert result.mode == "replay"
    assert result.provider == "replay"
    with pytest.raises(ModelError, match="no replay"):
        adapter.complete(ModelRequest("missing", {}))


def test_live_adapter_requires_approval_and_config(monkeypatch) -> None:
    monkeypatch.delenv("META_SPEND_APPROVED", raising=False)
    adapter = MetaLiveAdapter(api_url="http://x", api_key="k", model="m")
    with pytest.raises(ModelError, match="explicit spending approval"):
        adapter.complete(ModelRequest("p", {}), approved=False)
    with pytest.raises(ModelError, match="live call refused"):
        adapter.complete(ModelRequest("p", {}), approved=True)
    monkeypatch.setenv("META_SPEND_APPROVED", "1")
    bare = MetaLiveAdapter(api_url="", api_key="", model="")
    monkeypatch.delenv("META_API_URL", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)
    monkeypatch.delenv("META_MODEL", raising=False)
    with pytest.raises(ModelError, match="not configured"):
        bare.complete(ModelRequest("p", {}), approved=True)


def test_live_adapter_success_and_usage(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("META_SPEND_APPROVED", "1")

    class _Resp:
        status_code = 200

        def json(self) -> Any:
            return {
                "choices": [{"message": {"content": '{"intent": "help", "args": {}}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }

    seen: dict[str, Any] = {}

    def _post(url: str, **kwargs: Any) -> _Resp:
        seen["url"] = url
        seen["auth"] = kwargs["headers"]["Authorization"]
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)
    adapter = MetaLiveAdapter(api_url="http://meta.test/v1", api_key="secret", model="llama-mvp")
    result = adapter.complete(ModelRequest("interpret_command", {"text": "hi"}), approved=True)
    assert result.mode == "live"
    assert result.provider == "meta"
    assert result.model == "llama-mvp"
    assert result.result == {"intent": "help", "args": {}}
    assert result.usage["total_tokens"] == 15
    assert result.request_digest
    assert seen["auth"] == "Bearer secret"
    assert "secret" not in str(result.as_record())


def test_live_adapter_provider_failures(monkeypatch) -> None:
    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("META_SPEND_APPROVED", "1")
    adapter = MetaLiveAdapter(api_url="http://meta.test/v1", api_key="k", model="m")

    class _Bad:
        status_code = 500

        def json(self) -> Any:
            return {}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Bad())
    with pytest.raises(ModelError, match="HTTP 500"):
        adapter.complete(ModelRequest("p", {}), approved=True)

    class _Garbled:
        status_code = 200

        def json(self) -> Any:
            return {"choices": [{"message": {"content": "not json"}}]}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Garbled())
    with pytest.raises(ModelError, match="not valid JSON"):
        adapter.complete(ModelRequest("p", {}), approved=True)


def test_read_only_blocks_consequential_intents() -> None:
    store = DemoStore()
    with pytest.raises(WondrueError, match="blocked in read-only"):
        handle_command(
            mode="read-only", max_mode="read-only", text="", target="demo",
            intent_override={"intent": "start_demo", "args": {}},
            store=store,
        )
    assert store.runs == {}
    result = handle_command(
        mode="read-only", max_mode="read-only", text="", target="demo",
        intent_override={"intent": "help", "args": {}},
        store=store,
    )
    assert result["outcome"] == "result"


def test_ask_previews_then_executes_on_approval() -> None:
    store = DemoStore()
    preview = handle_command(
        mode="ask", max_mode="ask", text="", target="demo",
        intent_override={"intent": "start_demo", "args": {"run_key": "demo-t1"}},
        store=store,
    )
    assert preview["outcome"] == "preview"
    assert "cost_bounds" in preview["preview"]
    assert store.runs == {}  # nothing executed
    done = handle_command(
        mode="ask", max_mode="ask", text="", target="demo",
        intent_override={"intent": "start_demo", "args": {}},
        approval_token=preview["approval_token"],
        store=store,
    )
    assert done["outcome"] == "result"
    assert done["data"]["summary"]["decision"] == "authorized"
    assert store.runs["demo-t1"]["stage"] == "done"


def test_ask_rejects_bad_and_mismatched_tokens() -> None:
    store = DemoStore()
    with pytest.raises(WondrueError, match="invalid or expired"):
        handle_command(
            mode="ask", max_mode="ask", text="", target="demo",
            intent_override={"intent": "start_demo", "args": {}},
            approval_token="bogus", store=store,
        )
    preview = handle_command(
        mode="ask", max_mode="ask", text="", target="demo",
        intent_override={"intent": "start_demo", "args": {}},
        store=store,
    )
    with pytest.raises(WondrueError, match="does not match"):
        handle_command(
            mode="ask", max_mode="ask", text="", target="demo",
            intent_override={"intent": "stop", "args": {}},
            approval_token=preview["approval_token"], store=store,
        )


def test_full_executes_within_limits() -> None:
    store = DemoStore()
    result = handle_command(
        mode="full", max_mode="full", text="", target="demo",
        intent_override={"intent": "start_demo", "args": {"run_key": "demo-full1"}},
        store=store,
    )
    assert result["data"]["label"] == DEMO_LABEL


def test_demo_commands_need_no_database() -> None:
    result = handle_command(
        mode="full", max_mode="full", text="", target="demo",
        intent_override={"intent": "start_demo", "args": {"run_key": "demo-nodb"}},
        store=DemoStore(), conn=None,
    )
    assert result["outcome"] == "result"


def test_real_target_requires_connection() -> None:
    with pytest.raises(WondrueError, match="database connection"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            intent_override={
                "intent": "inspect_candidate",
                "args": {"run_id": "00000000-0000-0000-0000-000000000000"},
            },
            conn=None,
        )
    answered = handle_command(
        mode="full", max_mode="full", text="", target="real",
        intent_override={"intent": "help", "args": {}},
        conn=None,
    )
    assert answered["outcome"] == "result"


def test_inspect_review_reads_persisted_record(monkeypatch) -> None:
    from types import SimpleNamespace

    from app import workflow_db

    seen: dict[str, str] = {}

    def _get_record(conn: object, rid: object, kind: str, key: str) -> object:
        seen["kind"] = kind
        seen["key"] = key
        return SimpleNamespace(payload={"reason": "association ambiguous"})

    monkeypatch.setattr(workflow_db, "get_record_in_transaction", _get_record)
    result = handle_command(
        mode="read-only", max_mode="full", text="", target="real",
        intent_override={
            "intent": "inspect_review",
            "args": {"run_id": "00000000-0000-0000-0000-000000000001"},
        },
        conn=object(),
    )
    assert result["outcome"] == "result"
    assert result["data"]["review"] == {"reason": "association ambiguous"}
    assert (seen["kind"], seen["key"]) == ("identity_review", "dossier-association")


def test_unknown_mode_target_rejected() -> None:
    with pytest.raises(WondrueError, match="unknown mode"):
        handle_command(mode="root", max_mode="full", text="", intent_override={"intent": "help", "args": {}})
    with pytest.raises(WondrueError, match="unknown target"):
        handle_command(
            mode="full", max_mode="full", text="", target="prod",
            intent_override={"intent": "help", "args": {}},
        )


def test_compare_uses_validated_proposal_and_python_math() -> None:
    site = {
        name: {"value": value, "unit": "t", "provenance": "t", "classification": "assumed"}
        for name, value in {
            "usable_area_m2": 100.0, "electricity_usd_per_kwh": 0.12,
            "water_usd_per_l": 0.002, "labor_h_per_m2_week": 0.1,
            "labor_usd_per_h": 15.0, "hvac_usd_per_m2_year": 8.0,
            "other_opex_usd_per_m2_year": 5.0,
        }.items()
    }
    result = compare_with_proposal(
        {"options": [{"config": "bench"}, {"config": "rack3"}], "note": "t"}, site
    )
    assert [c["config"] for c in result["comparisons"]] == ["bench", "rack3"]
    assert all(c["complete"] for c in result["comparisons"])
    assert result["comparisons"][1]["outputs"]["sites"] == 7500.0
    # Michael's verdict rides along: price/nutrients missing here.
    assert all(c["financial_complete"] is False for c in result["comparisons"])
    assert all(
        c["financial_verdict"] == "insufficient_evidence" for c in result["comparisons"]
    )


def test_compare_financial_override_reaches_michael() -> None:
    site = {
        "usable_area_m2": {"value": 100.0, "unit": "m2"},
        "price_per_kg": {"value": 8.0, "unit": "USD/kg"},
        "nutrient_usd_per_l": {"value": 0.05, "unit": "USD/L"},
        "electricity_usd_per_kwh": {"value": 0.12, "unit": "USD/kWh"},
        "water_usd_per_l": {"value": 0.002, "unit": "USD/L"},
        "labor_h_per_m2_week": {"value": 0.1, "unit": "h/m2/week"},
        "labor_usd_per_h": {"value": 15.0, "unit": "USD/h"},
        "hvac_usd_per_m2_year": {"value": 8.0, "unit": "USD/m2/year"},
        "other_opex_usd_per_m2_year": {"value": 5.0, "unit": "USD/m2/year"},
    }
    result = compare_with_proposal(
        {"options": [{"config": "bench", "overrides": {"price_per_kg": 9.0}}],
         "note": "t"},
        site,
    )
    bench = result["comparisons"][0]
    assert bench["financial_complete"] is True
    assert bench["financial_verdict"] == "favorable_under_assumptions"
    assert bench["financial_outputs"]["operating_net_usd_per_year"] == "21640.40"
    assert "annual_net_usd" not in bench["outputs"]  # engineering carries no money


def test_synthetic_gate_blocks_demo_after_evidenced_pilot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.pilot as pilot_module

    monkeypatch.setenv("EMERGENCY_DIAGNOSTIC_DEMO", "0")
    monkeypatch.setattr(pilot_module, "pilot_success_evidenced", lambda config=None: True)
    with pytest.raises(WondrueError, match="unavailable through ordinary selection"):
        handle_command(
            mode="full", max_mode="full", text="", target="demo",
            intent_override={"intent": "start_demo", "args": {"run_key": "demo-g1"}},
            store=DemoStore(), conn=None,
        )
    monkeypatch.setenv("EMERGENCY_DIAGNOSTIC_DEMO", "1")
    result = handle_command(
        mode="full", max_mode="full", text="", target="demo",
        intent_override={"intent": "start_demo", "args": {"run_key": "demo-g1"}},
        store=DemoStore(), conn=None,
    )
    assert result["outcome"] == "result"


def test_route_read_only_forbids_demo_start() -> None:
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={"mode": "read-only", "target": "demo", "intent": {"intent": "start_demo", "args": {}}},
    )
    assert resp.status_code == 403


def test_route_ask_preview_and_approve() -> None:
    preview = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={
            "mode": "ask", "target": "demo",
            "intent": {"intent": "start_demo", "args": {"run_key": "demo-route1"}},
        },
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["outcome"] == "preview"
    assert DEMO.runs == {}
    done = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={
            "mode": "ask", "target": "demo",
            "intent": {"intent": "start_demo", "args": {}},
            "approval_token": body["approval_token"],
        },
    )
    assert done.status_code == 200
    assert done.json()["data"]["summary"]["decision"] == "authorized"


def test_route_full_demo_and_labels() -> None:
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={
            "mode": "full", "target": "demo",
            "intent": {"intent": "start_demo", "args": {"run_key": "demo-route2"}},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["label"] == DEMO_LABEL
    assert body["data"]["summary"]["synthetic"] is True


def test_route_unknown_intent_rejected() -> None:
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={"mode": "full", "target": "demo", "intent": {"intent": "launch", "args": {}}},
    )
    assert resp.status_code == 422


def test_route_demo_reset() -> None:
    client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={
            "mode": "full", "target": "demo",
            "intent": {"intent": "start_demo", "args": {"run_key": "demo-reset1"}},
        },
    )
    assert DEMO.runs
    resp = client.post("/wondrue/demo/reset", headers=_OPERATOR_HEADERS)
    assert resp.status_code == 200
    assert DEMO.runs == {}


def test_route_real_start_without_database_is_503(monkeypatch) -> None:
    from app.db import DatabaseUnavailableError

    monkeypatch.setenv("MVP_WORKFLOW_ENABLED", "1")

    def _boom(**kwargs: Any) -> Any:
        raise DatabaseUnavailableError("database_unavailable")

    monkeypatch.setattr(
        "app.observability.transactions.run_consequential_transaction", _boom
    )
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={
            "mode": "full", "target": "real",
            "intent": {
                "intent": "start_real_evaluation",
                "args": {"assignment_id": "a1", "query": "q"},
            },
        },
    )
    assert resp.status_code == 503


class _RouteConn:
    """Connection double recording transaction ownership calls."""

    def __init__(self, *, commit_error: Exception | None = None) -> None:
        self.writes = 0
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self._commit_error = commit_error

    def cursor(self) -> Any:
        conn = self

        class _Cur:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def execute(self, *args: Any, **kwargs: Any) -> None:
                conn.writes += 1

        return _Cur()

    def commit(self) -> None:
        self.commits += 1
        if self._commit_error is not None:
            raise self._commit_error

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1


def test_route_rolls_back_when_command_writes_then_raises(monkeypatch) -> None:
    import app.routes.wondrue as wondrue_routes
    from app.wondrue import WondrueError

    conns: list[_RouteConn] = []

    def _connect() -> _RouteConn:
        conn = _RouteConn()
        conns.append(conn)
        return conn

    def _write_then_raise(**kwargs: Any) -> Any:
        with kwargs["conn"].cursor() as cur:
            cur.execute("INSERT INTO t VALUES (1)")
        raise WondrueError("boom")

    monkeypatch.setattr(wondrue_routes, "get_connection", _connect)
    monkeypatch.setattr(wondrue_routes, "handle_command", _write_then_raise)
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={"mode": "full", "target": "real", "text": "hi"},
    )
    assert resp.status_code == 400
    (conn,) = conns
    assert conn.writes == 1
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert conn.closes == 1


def test_route_failed_commit_is_503_with_rollback(monkeypatch) -> None:
    import app.routes.wondrue as wondrue_routes

    conns: list[_RouteConn] = []

    def _connect() -> _RouteConn:
        conn = _RouteConn(commit_error=ConnectionError("disk on fire"))
        conns.append(conn)
        return conn

    monkeypatch.setattr(wondrue_routes, "get_connection", _connect)
    monkeypatch.setattr(
        wondrue_routes, "handle_command", lambda **kwargs: {"ok": True}
    )
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={"mode": "full", "target": "real", "text": "hi"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "database_unavailable"
    (conn,) = conns
    assert conn.commits == 1
    assert conn.rollbacks == 1
    assert conn.closes == 1


def test_route_commits_once_on_success(monkeypatch) -> None:
    import app.routes.wondrue as wondrue_routes

    conns: list[_RouteConn] = []

    def _connect() -> _RouteConn:
        conn = _RouteConn()
        conns.append(conn)
        return conn

    monkeypatch.setattr(wondrue_routes, "get_connection", _connect)
    monkeypatch.setattr(
        wondrue_routes, "handle_command", lambda **kwargs: {"ok": True}
    )
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={"mode": "full", "target": "real", "text": "hi"},
    )
    assert resp.status_code == 200
    (conn,) = conns
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.closes == 1


def test_route_real_read_without_database_is_503(monkeypatch) -> None:
    import app.routes.wondrue as wondrue_routes
    from app.db import DatabaseNotConfiguredError

    def _boom() -> Any:
        raise DatabaseNotConfiguredError("database_not_configured")

    monkeypatch.setattr(wondrue_routes, "get_connection", _boom)
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={
            "mode": "read-only", "target": "real",
            "intent": {"intent": "help", "args": {}},
        },
    )
    # help needs no database and still answers.
    assert resp.status_code == 200
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={
            "mode": "read-only", "target": "real",
            "intent": {
                "intent": "inspect_candidate",
                "args": {"run_id": "00000000-0000-0000-0000-000000000000", "key": "main"},
            },
        },
    )
    assert resp.status_code == 503


def test_grant_caps_caller_mode_direct() -> None:
    with pytest.raises(WondrueAuthError, match="exceeds grant"):
        handle_command(
            mode="full", max_mode="read-only", text="", target="demo",
            intent_override={"intent": "help", "args": {}},
            store=DemoStore(), conn=None,
        )
    # Unknown grant also fails closed.
    with pytest.raises(WondrueAuthError, match="unknown grant"):
        handle_command(
            mode="read-only", max_mode="superuser", text="", target="demo",
            intent_override={"intent": "help", "args": {}},
            store=DemoStore(), conn=None,
        )


def test_route_reader_cannot_use_full_mode() -> None:
    resp = client.post(
        "/wondrue/command",
        headers=_READER_HEADERS,
        json={"mode": "full", "target": "demo", "intent": {"intent": "help", "args": {}}},
    )
    assert resp.status_code == 403
    resp = client.post(
        "/wondrue/command",
        headers=_READER_HEADERS,
        json={"mode": "read-only", "target": "demo", "intent": {"intent": "help", "args": {}}},
    )
    assert resp.status_code == 200


def test_route_unknown_key_rejected() -> None:
    resp = client.post(
        "/wondrue/command",
        headers={"X-Wondrue-Key": "bogus"},
        json={"mode": "read-only", "target": "demo", "intent": {"intent": "help", "args": {}}},
    )
    assert resp.status_code == 401


def test_route_missing_key_rejected() -> None:
    resp = client.post(
        "/wondrue/command",
        json={"mode": "read-only", "target": "demo", "intent": {"intent": "help", "args": {}}},
    )
    assert resp.status_code == 401


def test_route_unconfigured_keys_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WONDRUE_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("WONDRUE_READER_KEY", raising=False)
    resp = client.post(
        "/wondrue/command",
        headers=_OPERATOR_HEADERS,
        json={"mode": "read-only", "target": "demo", "intent": {"intent": "help", "args": {}}},
    )
    assert resp.status_code == 503


def test_route_demo_reset_requires_auth() -> None:
    resp = client.post("/wondrue/demo/reset")
    assert resp.status_code == 401
    resp = client.post("/wondrue/demo/reset", headers=_READER_HEADERS)
    assert resp.status_code == 403


def test_route_page_marks_modes() -> None:
    resp = client.get("/wondrue")
    assert resp.status_code == 200
    text = resp.text
    assert DEMO_LABEL in text
    assert "REAL-PROPERTY EVALUATION" in text
    assert "model: live" in text or "modelBadge" in text
