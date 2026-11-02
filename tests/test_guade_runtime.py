"""Offline GUADE runtime tests. All providers/subprocesses are faked.

No paid call, no real `muse exec` meta dispatch, and no database access
can occur here: transports and runners are injected test doubles.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from guade.runtime import (Authorization, BudgetExhausted, Coach,
                           CoachUnavailable, Dispatcher, Ledger,
                           parse_jsonl, run_cycle)

ROOT = Path(__file__).resolve().parent.parent


def _ledger(tmp_path, **overrides) -> Ledger:
    return Ledger(tmp_path / "state", Authorization(**overrides))


def _transport(decisions, calls):
    def transport(payload, api_key, timeout):
        calls.append(payload)
        text = decisions[min(len(calls) - 1, len(decisions) - 1)]
        return {"choices": [{"message": {"content": text}}],
                "usage": {"total_tokens": 100}}
    return transport


def _task(tmp_path, verify=None):
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the harmless thing", encoding="utf-8")
    return {"id": "t1", "title": "demo", "prompt": "do the harmless thing",
            "prompt_file": str(prompt), "test_command": ["fake-tests"],
            "allowed_files": ["result.md"],
            "verify": verify}


def _ok_runner(cmd, timeout, cwd):
    (Path(cwd) / "result.md").write_text("result\n", encoding="utf-8")
    return SimpleNamespace(returncode=0, stdout='{"payload":{"kind":"x"}}\n',
                           stderr="")


def test_authorization_matches_owner_envelope():
    data = json.loads((ROOT / "guade" / "config.example.json")
                      .read_text(encoding="utf-8"))
    auth = Authorization.from_dict(data)
    assert (auth.max_wonder_dispatches, auth.max_steps_per_task,
            auth.max_correction_retries) == (3, 20, 1)
    assert auth.baseten_budget_usd_cap == 10.0


def test_authorization_rejects_unknown_fields():
    with pytest.raises(Exception):
        Authorization.from_dict({"surprise": 1})


def test_cycle_completes_within_budget(tmp_path):
    ledger = _ledger(tmp_path)
    calls: list = []
    coach = Coach(ledger, transport=_transport(["APPROVE - ok"] * 3, calls),
                  api_key="test-key")
    dispatcher = Dispatcher(ledger, runner=_ok_runner)
    result = run_cycle(_task(tmp_path), coach, dispatcher, ledger,
                       tmp_path, test_runner=_ok_runner)
    assert result["status"] == "completed"
    assert ledger.counters["wonder_dispatches"] == 1
    assert ledger.counters["coach_calls"] == 2
    audit = (tmp_path / "state" / "audit.jsonl").read_text(encoding="utf-8")
    assert "cycle_finished" in audit and "completed" in audit


def test_coach_stop_blocks_dispatch(tmp_path):
    ledger = _ledger(tmp_path)
    coach = Coach(ledger, transport=_transport(["STOP - unsafe"], []),
                  api_key="test-key")

    def forbidden(cmd, timeout, cwd):
        raise AssertionError("dispatcher must not run after STOP")

    result = run_cycle(_task(tmp_path), coach,
                       Dispatcher(ledger, runner=forbidden), ledger,
                       tmp_path, test_runner=_ok_runner)
    assert result["status"] == "blocked_coach_stop"
    assert ledger.counters["wonder_dispatches"] == 0


def test_retry_bound_enforced(tmp_path):
    ledger = _ledger(tmp_path)
    coach = Coach(ledger, transport=_transport(
        ["APPROVE", "REVISE - fix it", "REVISE - still bad"], []), api_key="k")

    def failing(cmd, timeout, cwd):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    result = run_cycle(_task(tmp_path), coach,
                       Dispatcher(ledger, runner=_ok_runner), ledger,
                       tmp_path, test_runner=failing)
    assert result["status"] == "failed"
    assert ledger.counters["wonder_dispatches"] == 2
    assert ledger.counters["correction_retries"] == 1


def test_dispatch_cap_fail_closed(tmp_path):
    ledger = _ledger(tmp_path, max_wonder_dispatches=1)
    dispatcher = Dispatcher(ledger, runner=_ok_runner)
    prompt = tmp_path / "p.md"
    prompt.write_text("x", encoding="utf-8")
    dispatcher.dispatch(prompt, tmp_path)
    with pytest.raises(BudgetExhausted):
        dispatcher.dispatch(prompt, tmp_path)


def test_windows_meta_invocation_preserves_provider_value(tmp_path,
                                                           monkeypatch):
    """Regression: the failed live trial built `--provider --model ... meta`.

    Drives the real Windows path (cmd /c + muse.cmd shim) with a faked
    subprocess runner; no provider is contacted.
    """
    ledger = _ledger(tmp_path)
    captured = {}

    def fake_runner(cmd, timeout, cwd):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("guade.runtime.shutil.which",
                        lambda name: r"C:\Tools\muse.cmd")
    monkeypatch.setattr("guade.runtime._subprocess_runner", fake_runner)
    dispatcher = Dispatcher(ledger)
    # Exercise Windows command construction without requiring a real mission
    # or process: this dispatcher uses a monkeypatched offline subprocess.
    ledger.check_allowed = lambda *args, **kwargs: None
    prompt = tmp_path / "p.md"
    prompt.write_text("x", encoding="utf-8")
    dispatcher.dispatch(prompt, tmp_path)
    cmd = captured["cmd"]
    assert cmd[:3] == ["cmd", "/c", r"C:\Tools\muse.cmd"]
    index = cmd.index("--provider")
    assert cmd[index:index + 4] == ["--provider", "meta",
                                    "--model", "muse-wonder-1.0"]


def test_failed_dispatch_cannot_complete(tmp_path):
    """Preflight: pytest passing must not rescue a failed Wonder dispatch."""
    ledger = _ledger(tmp_path)
    coach = Coach(ledger, transport=_transport(["APPROVE"] * 4, []),
                  api_key="k")

    def bad_runner(cmd, timeout, cwd):
        return SimpleNamespace(returncode=2, stdout="", stderr="cli error")

    result = run_cycle(_task(tmp_path), coach,
                       Dispatcher(ledger, runner=bad_runner), ledger,
                       tmp_path, test_runner=_ok_runner)
    assert result["status"] == "failed"


def test_exhausted_retry_allowance_never_redispatches(tmp_path):
    """Preflight: with retries already consumed, no second dispatch occurs."""
    ledger = _ledger(tmp_path)
    ledger.reserve_retry()
    coach = Coach(ledger, transport=_transport(["APPROVE", "REVISE - fix"], []),
                  api_key="k")

    def failing(cmd, timeout, cwd):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    result = run_cycle(_task(tmp_path), coach,
                       Dispatcher(ledger, runner=_ok_runner), ledger,
                       tmp_path, test_runner=failing)
    assert result["status"] == "failed"
    assert ledger.counters["wonder_dispatches"] == 1


def test_coach_unavailable_fail_closed(tmp_path):
    ledger = _ledger(tmp_path)
    coach = Coach(ledger, transport=_transport(["APPROVE"], []), api_key="")
    assert not coach.available()
    with pytest.raises(CoachUnavailable):
        coach.decide("assignment", "anything")
    assert ledger.counters["coach_calls"] == 0


def test_coach_token_ceiling_fail_closed(tmp_path):
    from dataclasses import replace
    ledger = _ledger(tmp_path, max_coach_total_tokens=10000,
                     coach_max_tokens_per_call=1024)
    coach = Coach(ledger, transport=_transport(["APPROVE"] * 3, []),
                  api_key="k")
    coach.decide("assignment", "one")
    reserved = next(json.loads(line)["reserved_units"]
                    for line in ledger.audit_path.read_text().splitlines()
                    if json.loads(line)["event"] == "coach_call_reserved")
    ledger.authorization = replace(ledger.authorization,
                                   max_coach_total_tokens=reserved + 50)
    with pytest.raises(BudgetExhausted):
        coach.decide("review", "two")


def test_malformed_coach_response_fails_closed(tmp_path):
    ledger = _ledger(tmp_path)
    coach = Coach(ledger, transport=lambda p, k, t: {}, api_key="k")
    with pytest.raises(CoachUnavailable):
        coach.decide("assignment", "x")


def _raw_coach(tmp_path, response):
    ledger = _ledger(tmp_path)
    coach = Coach(ledger, transport=lambda p, k, t: response, api_key="k")
    return ledger, coach


def _chat(message, usage=50):
    return {"choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"total_tokens": usage}}


def test_coach_null_content_is_stop_not_crash(tmp_path):
    """Regression: the live trial crashed on content=None (AttributeError)."""
    ledger, coach = _raw_coach(tmp_path, _chat({"content": None}))
    result = coach.decide("assignment", "x")
    assert result["decision"] == "STOP"
    assert ledger.counters["coach_calls"] == 1
    assert ledger.counters["coach_total_tokens"] == 50


def test_coach_missing_and_empty_content_are_stop(tmp_path):
    for message in ({}, {"content": ""}, {"content": "   "},
                    {"content": None, "tool_calls": [{"id": "1"}]}):
        ledger, coach = _raw_coach(tmp_path, _chat(message))
        assert coach.decide("assignment", "x")["decision"] == "STOP"


def test_coach_content_parts_are_joined(tmp_path):
    message = {"content": [{"type": "text", "text": "APPROVE"},
                           {"type": "text", "text": " - proceed"}]}
    _ledger_unused, coach = _raw_coach(tmp_path, _chat(message))
    assert coach.decide("assignment", "x")["decision"] == "APPROVE"


def test_coach_reasoning_only_never_authorizes(tmp_path):
    """Reasoning is diagnostics only; it must not produce a decision."""
    for reasoning in ("Analysis complete. REVISE the plan.", "APPROVE"):
        message = {"content": None, "reasoning_content": reasoning}
        _unused, coach = _raw_coach(tmp_path, _chat(message))
        assert coach.decide("review", "x")["decision"] == "STOP"


def test_coach_token_exhaustion_is_not_completion(tmp_path):
    """finish_reason=length is exhaustion, never a valid final answer."""
    for message in ({"content": "APPROVE done"}, {"content": None}):
        response = {"choices": [{"message": message,
                                 "finish_reason": "length"}],
                    "usage": {"total_tokens": 1100}}
        ledger, coach = _raw_coach(tmp_path, response)
        assert coach.decide("assignment", "x")["decision"] == "STOP"
        audit = (tmp_path / "state" / "audit.jsonl").read_text(
            encoding="utf-8")
        assert '"exhausted": true' in audit


def test_coach_missing_final_output_is_stop(tmp_path):
    response = {"choices": [{"message": {}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 40}}
    _unused, coach = _raw_coach(tmp_path, response)
    assert coach.decide("assignment", "x")["decision"] == "STOP"


def test_coach_ambiguous_decisions_are_stop(tmp_path):
    message = {"content": "Do not APPROVE this; REVISE instead."}
    _unused, coach = _raw_coach(tmp_path, _chat(message))
    assert coach.decide("review", "x")["decision"] == "STOP"


def test_coach_word_boundaries_avoid_substring_approval(tmp_path):
    message = {"content": "The change was approved by tests; STOP here."}
    _unused, coach = _raw_coach(tmp_path, _chat(message))
    assert coach.decide("review", "x")["decision"] == "STOP"


def test_decision_defaults_to_stop(tmp_path):
    ledger = _ledger(tmp_path)
    coach = Coach(ledger, transport=_transport(["no decision here"], []),
                  api_key="k")
    assert coach.decide("assignment", "x")["decision"] == "STOP"


def test_demo_verify_requires_actual_modification(tmp_path, monkeypatch):
    """Preflight: a pre-existing unchanged document is not task evidence."""
    from guade import cli

    doc = tmp_path / "guade-activation-trial.md"
    monkeypatch.setattr(cli, "DEMO_DOC", doc)
    doc.write_text("GUADE P1 SearXNG Brave Qwen", encoding="utf-8")
    verify = cli._verify_demo_doc(cli._demo_doc_hash())
    ok, _detail = verify()
    assert not ok
    doc.write_text("GUADE P1 SearXNG Brave Qwen updated", encoding="utf-8")
    ok, _detail = verify()
    assert ok


def test_parse_jsonl_tolerant():
    parsed = parse_jsonl('noise\n{"stream":{"kind":"session","id":"s1"},'
                         '"payload":{"kind":"run_started"}}\n{bad json\n')
    assert parsed["events"] == 1
    assert parsed["session_id"] == "s1"
    assert parsed["kinds"] == ["run_started"]
