"""Wondrue package/simulation/discovery operations (mode + grant gated)."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

import pytest

from app import discovery_runner as runner_module
from app import priscilla as priscilla_module
from app import uriel as uriel_module
from app.demo import DemoStore
from app.main import app
from app.wondrue import WondrueError, handle_command

client = TestClient(app, raise_server_exceptions=False)

_OPERATOR_HEADERS = {"X-Wondrue-Key": "operator-secret"}
_READER_HEADERS = {"X-Wondrue-Key": "reader-secret"}
_CONN = object()


@pytest.fixture(autouse=True)
def _keys(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("WONDRUE_OPERATOR_KEY", "operator-secret")
    monkeypatch.setenv("WONDRUE_READER_KEY", "reader-secret")
    monkeypatch.delenv("EMERGENCY_DIAGNOSTIC_DEMO", raising=False)
    yield


def _override(intent: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"intent": intent, "args": args or {}}


def _cells_arg() -> str:
    return json.dumps(
        [
            {
                "county": "dallas county",
                "category": "high_rise",
                "bbox": [32.0, -97.0, 33.0, -96.0],
            }
        ]
    )


def test_inspect_packages_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _list(conn: Any, limit: int = 50) -> list[dict[str, Any]]:
        seen["limit"] = limit
        return [{"entry_id": "e1", "package_state": "prepared"}]

    monkeypatch.setattr(uriel_module, "list_packages_in_transaction", _list)
    result = handle_command(
        mode="read-only", max_mode="read-only", text="", target="real",
        intent_override=_override("inspect_packages", {"limit": 10}), conn=_CONN,
    )
    assert result["data"]["packages"] == [{"entry_id": "e1", "package_state": "prepared"}]
    assert seen == {"limit": 10}
    with pytest.raises(WondrueError, match="database connection"):
        handle_command(
            mode="read-only", max_mode="full", text="", target="real",
            intent_override=_override("inspect_packages"), conn=None,
        )
    with pytest.raises(WondrueError, match="limit must be"):
        handle_command(
            mode="read-only", max_mode="full", text="", target="real",
            intent_override=_override("inspect_packages", {"limit": 500}), conn=_CONN,
        )


def test_preview_simulation_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def _preview(conn: Any, **kwargs: Any) -> dict[str, Any]:
        return {"expected": {"cohort": 40}, "kwargs": kwargs}

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("preview must not execute")

    monkeypatch.setattr(priscilla_module, "preview_simulation_in_transaction", _preview)
    monkeypatch.setattr(priscilla_module, "run_simulation_in_transaction", _boom)
    result = handle_command(
        mode="read-only", max_mode="read-only", text="", target="real",
        intent_override=_override("preview_simulation", {"cohort": "c"}), conn=_CONN,
    )
    assert result["data"]["expected"] == {"cohort": 40}


def test_run_simulation_modes_and_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _run(conn: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"sim_id": "s1", "replayed": False}

    monkeypatch.setattr(priscilla_module, "run_simulation_in_transaction", _run)
    override = _override("run_simulation", {"cohort": "c", "seed": "s"})
    with pytest.raises(WondrueError, match="blocked in read-only"):
        handle_command(
            mode="read-only", max_mode="read-only", text="", target="real",
            intent_override=override, conn=_CONN,
        )
    preview = handle_command(
        mode="ask", max_mode="full", text="", target="real",
        intent_override=override, conn=_CONN,
    )
    assert preview["outcome"] == "preview"
    assert preview["preview"]["cost_bounds"] == {"brave_attempts": 0, "meta_calls": 0}
    assert calls == []
    executed = handle_command(
        mode="ask", max_mode="full", text="", target="real",
        approval_token=preview["approval_token"],
        intent_override=override, conn=_CONN,
    )
    assert executed["data"]["simulation"]["sim_id"] == "s1"
    assert calls and calls[0]["actor"] == "wondrue"
    assert calls[0]["idempotency_key"] == "wondrue:c:s"
    assert calls[0]["package_entries"] is None


def test_inspect_simulation_returns_run_and_funnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        priscilla_module, "get_simulation_in_transaction",
        lambda conn, sim: {"sim_id": str(sim), "counts": {}},
    )
    monkeypatch.setattr(
        priscilla_module, "funnel_in_transaction",
        lambda conn, simulation_id=None: {"simulated_unique": {"outreach": 1}},
    )
    result = handle_command(
        mode="read-only", max_mode="read-only", text="", target="real",
        intent_override=_override("inspect_simulation", {"simulation_id": str(uuid4())}),
        conn=_CONN,
    )
    assert result["data"]["funnel"]["simulated_unique"]["outreach"] == 1
    monkeypatch.setattr(
        priscilla_module, "get_simulation_in_transaction",
        lambda conn, sim: None,
    )
    with pytest.raises(WondrueError, match="unknown simulation"):
        handle_command(
            mode="read-only", max_mode="full", text="", target="real",
            intent_override=_override("inspect_simulation", {"simulation_id": str(uuid4())}),
            conn=_CONN,
        )


def test_inspect_discovery_needs_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner_module, "latest_run_in_transaction",
        lambda conn, scope: {"scope": scope, "status": "completed"},
    )
    result = handle_command(
        mode="read-only", max_mode="read-only", text="", target="real",
        intent_override=_override("inspect_discovery", {"scope": "pilot"}), conn=_CONN,
    )
    assert result["data"]["run"]["status"] == "completed"
    with pytest.raises(WondrueError, match="scope is required"):
        handle_command(
            mode="read-only", max_mode="full", text="", target="real",
            intent_override=_override("inspect_discovery"), conn=_CONN,
        )


def test_start_discovery_burst_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _burst(get_conn: Any, config: Any) -> dict[str, Any]:
        captured["config"] = config
        return {"outcome": "completed", "passes": 1}

    monkeypatch.setattr(runner_module, "run_worker", _burst)
    result = handle_command(
        mode="full", max_mode="full", text="", target="real",
        intent_override=_override(
            "start_discovery",
            {"scope": "pilot", "policy": "pilot", "cells": _cells_arg()},
        ),
        conn=_CONN,
    )
    assert result["data"]["burst"]["outcome"] == "completed"
    assert captured["config"].max_passes == 1
    assert captured["config"].cells[0].county == "dallas county"
    with pytest.raises(WondrueError, match="max_passes must be"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            intent_override=_override(
                "start_discovery",
                {"scope": "pilot", "cells": _cells_arg(), "max_passes": 6},
            ),
            conn=_CONN,
        )
    with pytest.raises(WondrueError, match="cells"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            intent_override=_override("start_discovery", {"scope": "pilot"}),
            conn=_CONN,
        )
    with pytest.raises(WondrueError, match="blocked in read-only"):
        handle_command(
            mode="read-only", max_mode="read-only", text="", target="real",
            intent_override=_override(
                "resume_discovery", {"scope": "pilot", "cells": _cells_arg()}
            ),
            conn=_CONN,
        )


def test_stop_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runner_module, "request_stop_in_transaction",
        lambda conn, scope: scope == "pilot",
    )
    result = handle_command(
        mode="full", max_mode="full", text="", target="real",
        intent_override=_override("stop_discovery", {"scope": "pilot"}), conn=_CONN,
    )
    assert result["data"] == {"stop_requested": True}
    with pytest.raises(WondrueError, match="blocked in read-only"):
        handle_command(
            mode="read-only", max_mode="read-only", text="", target="real",
            intent_override=_override("stop_discovery", {"scope": "pilot"}), conn=_CONN,
        )


def test_new_intents_are_operational_only() -> None:
    for intent in (
        "inspect_packages", "preview_simulation", "run_simulation",
        "inspect_simulation", "inspect_discovery", "stop_discovery",
    ):
        with pytest.raises(WondrueError, match="not a demo intent"):
            handle_command(
                mode="read-only" if intent.startswith(("inspect", "preview")) else "full",
                max_mode="full", text="", target="demo",
                intent_override=_override(intent), store=DemoStore(), conn=None,
            )


def test_route_reader_can_inspect_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        uriel_module, "list_packages_in_transaction", lambda conn, limit=50: []
    )
    resp = client.post(
        "/wondrue/command",
        headers=_READER_HEADERS,
        json={
            "mode": "read-only", "target": "real",
            "intent": {"intent": "inspect_packages", "args": {}},
        },
    )
    # No database configured locally: the read reports unavailability
    # rather than executing, which still proves grant/mode gating passed.
    assert resp.status_code == 503

def test_start_discovery_acquire_operator_full_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _burst(get_conn: Any, config: Any) -> dict[str, Any]:
        captured["config"] = config
        return {"outcome": "completed", "passes": 1}

    monkeypatch.setattr(runner_module, "run_worker", _burst)
    result = handle_command(
        mode="full", max_mode="full", text="", target="real",
        intent_override=_override(
            "start_discovery",
            {"scope": "pilot", "cells": _cells_arg(), "acquire": True,
             "max_evaluations": 2},
        ),
        conn=_CONN,
    )
    assert result["data"]["burst"]["outcome"] == "completed"
    assert captured["config"].acquire_approved is True
    assert captured["config"].max_evaluations_per_pass == 2


def test_start_discovery_acquire_defaults_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _burst(get_conn: Any, config: Any) -> dict[str, Any]:
        captured["config"] = config
        return {"outcome": "completed", "passes": 1}

    monkeypatch.setattr(runner_module, "run_worker", _burst)
    handle_command(
        mode="full", max_mode="full", text="", target="real",
        intent_override=_override(
            "start_discovery", {"scope": "pilot", "cells": _cells_arg()}
        ),
        conn=_CONN,
    )
    assert captured["config"].acquire_approved is False


def test_start_discovery_acquire_ask_preview_then_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _burst(get_conn: Any, config: Any) -> dict[str, Any]:
        captured["config"] = config
        return {"outcome": "completed", "passes": 2}

    monkeypatch.setattr(runner_module, "run_worker", _burst)
    override = _override(
        "start_discovery",
        {"scope": "pilot", "cells": _cells_arg(), "acquire": True,
         "max_passes": 2, "max_evaluations": 3},
    )
    preview = handle_command(
        mode="ask", max_mode="full", text="", target="real",
        intent_override=override, conn=_CONN,
    )
    assert preview["outcome"] == "preview"
    assert preview["preview"]["inputs"]["acquire"] is True
    assert preview["preview"]["cost_bounds"] == {
        "brave_attempts": "<= 54", "meta_calls": 0
    }
    assert "acquisition" in preview["preview"]["effects"]
    assert captured == {}  # preview performs no work
    executed = handle_command(
        mode="ask", max_mode="full", text="", target="real",
        approval_token=preview["approval_token"],
        intent_override=override, conn=_CONN,
    )
    assert executed["data"]["burst"]["outcome"] == "completed"
    assert captured["config"].acquire_approved is True
    assert captured["config"].max_evaluations_per_pass == 3


def test_start_discovery_acquire_refused_without_operator_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.wondrue import WondrueAuthError

    def _boom(get_conn: Any, config: Any) -> dict[str, Any]:
        raise AssertionError("unapproved burst must not execute")

    monkeypatch.setattr(runner_module, "run_worker", _boom)
    override = _override(
        "start_discovery",
        {"scope": "pilot", "cells": _cells_arg(), "acquire": True},
    )
    preview = handle_command(
        mode="ask", max_mode="ask", text="", target="real",
        intent_override=override, conn=_CONN,
    )
    assert preview["outcome"] == "preview"  # preview itself is read-only
    with pytest.raises(WondrueAuthError, match="operator grant"):
        handle_command(
            mode="ask", max_mode="ask", text="", target="real",
            approval_token=preview["approval_token"],
            intent_override=override, conn=_CONN,
        )


def test_start_discovery_acquire_invalid_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(get_conn: Any, config: Any) -> dict[str, Any]:
        raise AssertionError("invalid burst must not execute")

    monkeypatch.setattr(runner_module, "run_worker", _boom)
    base: dict[str, Any] = {"scope": "pilot", "cells": _cells_arg()}
    for bad in ("maybe", 2, 2.5):
        with pytest.raises(WondrueError, match="acquire must be"):
            handle_command(
                mode="full", max_mode="full", text="", target="real",
                intent_override=_override(
                    "start_discovery", {**base, "acquire": bad}
                ),
                conn=_CONN,
            )
    for bad_bound in (-1, 6):
        with pytest.raises(WondrueError, match="max_evaluations must be"):
            handle_command(
                mode="full", max_mode="full", text="", target="real",
                intent_override=_override(
                    "start_discovery",
                    {**base, "acquire": True, "max_evaluations": bad_bound},
                ),
                conn=_CONN,
            )


def test_start_discovery_non_integral_bound_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(get_conn: Any, config: Any) -> dict[str, Any]:
        raise AssertionError("invalid burst must not execute")

    monkeypatch.setattr(runner_module, "run_worker", _boom)
    with pytest.raises(WondrueError, match="burst bounds must be numeric"):
        handle_command(
            mode="full", max_mode="full", text="", target="real",
            intent_override=_override(
                "start_discovery",
                {"scope": "pilot", "cells": _cells_arg(),
                 "acquire": True, "max_evaluations": 2.5},
            ),
            conn=_CONN,
        )


def test_start_discovery_read_only_makes_no_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(get_conn: Any, config: Any) -> dict[str, Any]:
        raise AssertionError("read-only must not execute")

    monkeypatch.setattr(runner_module, "run_worker", _boom)
    with pytest.raises(WondrueError, match="blocked in read-only"):
        handle_command(
            mode="read-only", max_mode="read-only", text="", target="real",
            intent_override=_override(
                "start_discovery",
                {"scope": "pilot", "cells": _cells_arg(), "acquire": True},
            ),
            conn=_CONN,
        )
