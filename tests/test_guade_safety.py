"""Runtime corrections: fake providers, temporary workspaces, no paid calls."""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from guade import cli
from guade.mission import Mission, MissionError, MissionLock
from guade.runtime import (Authorization, BudgetExhausted, Coach, CoachUnavailable,
                           Dispatcher, GuadeError, Ledger, ProcessUncertain, _decision_from_text,
                           _subprocess_runner, run_cycle)


def setup_cycle(tmp_path, answers=("APPROVE", "APPROVE"), dispatch=None):
    ledger = Ledger(tmp_path / "state", Authorization(max_correction_retries=0))
    prompt = ledger.state_dir / "prompt.md"
    prompt.write_text("Improve the specified local file only.")
    task = {"id": "bounded", "title": "bounded", "prompt": prompt.read_text(),
            "prompt_file": str(prompt), "allowed_files": ["result.md"],
            "test_command": ["fake-test"]}
    calls = []

    def transport(payload, key, timeout):
        calls.append(payload)
        return {"choices": [{"message": {"content": answers[min(len(calls)-1, len(answers)-1)]},
                             "finish_reason": "stop"}], "usage": {"total_tokens": 100}}

    def engineer(cmd, timeout, cwd):
        (cwd / "result.md").write_text("actual engineer change\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return (ledger, task, Coach(ledger, transport=transport, api_key="fake"),
            Dispatcher(ledger, runner=dispatch or engineer), calls)


def passing_tests(cmd, timeout, cwd):
    return SimpleNamespace(returncode=0, stdout="one test passed", stderr="")


@pytest.mark.parametrize("text", ["Do not APPROVE", "approved", "APPROVE proceed",
    "APPROVE\nSTOP", "APPROVE - then REVISE", "{\"decision\":\"APPROVE\"}", "", "approve"])
def test_strict_decision_refuses_narrative(text):
    assert _decision_from_text(text) == "STOP"


@pytest.mark.parametrize("decision", ["REVISE - narrow the task", "STOP", "Do not APPROVE"])
def test_nonapproval_never_dispatches(tmp_path, decision):
    def forbidden(*args):
        pytest.fail("dispatch must not occur")
    ledger, task, coach, dispatcher, calls = setup_cycle(tmp_path, (decision,), forbidden)
    result = run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    assert result["status"] == "blocked_coach_stop"
    assert ledger.counters["wonder_dispatches"] == 0


def test_scope_violation_stops_before_tests_or_review(tmp_path):
    def engineer(cmd, timeout, cwd):
        (cwd / "unrelated.txt").write_text("must be preserved for owner")
        return passing_tests(cmd, timeout, cwd)
    ledger, task, coach, dispatcher, calls = setup_cycle(tmp_path, dispatch=engineer)
    with pytest.raises(GuadeError):
        run_cycle(task, coach, dispatcher, ledger, tmp_path,
                  lambda *a: pytest.fail("tests must not run after scope violation"))
    assert len(calls) == 1
    assert (tmp_path / "unrelated.txt").exists()
    assert ledger.data["tasks"]["bounded"]["status"] == "blocked_error"


def test_review_receives_actual_diff_and_replay_refuses(tmp_path):
    ledger, task, coach, dispatcher, calls = setup_cycle(tmp_path)
    result = run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    assert result["status"] == "completed"
    review = calls[1]["messages"][1]["content"]
    assert task["prompt"] in review and "APPROVED ASSIGNMENT" in review
    assert "+actual engineer change" in review and "after/result.md" in review
    assert ledger.counters["wonder_dispatches"] == 1
    with pytest.raises(GuadeError):
        run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    assert len(calls) == 2


def test_test_runner_changes_also_checked(tmp_path):
    ledger, task, coach, dispatcher, calls = setup_cycle(tmp_path)
    def changed_test(cmd, timeout, cwd):
        (cwd / "bad.txt").write_text("bad")
        return passing_tests(cmd, timeout, cwd)
    with pytest.raises(GuadeError):
        run_cycle(task, coach, dispatcher, ledger, tmp_path, changed_test)
    assert len(calls) == 1


def test_missing_usage_keeps_prompt_and_output_reservation(tmp_path):
    ledger = Ledger(tmp_path, Authorization())
    coach = Coach(ledger, api_key="fake", transport=lambda *a: {
        "choices": [{"message": {"content": "APPROVE"}}]})
    coach.decide("assignment", "x" * 2000)
    assert ledger.counters["coach_total_tokens"] > 2000 + 1024
    before = ledger.counters["coach_total_tokens"]
    ledger.authorization = replace(ledger.authorization, max_coach_total_tokens=before)
    with pytest.raises(BudgetExhausted):
        coach.decide("review", "anything")


def test_provider_failure_retains_reservation_and_blocks_dispatch(tmp_path):
    ledger, task, coach, dispatcher, _ = setup_cycle(tmp_path)
    def fail(*a):
        raise RuntimeError("provider error")
    coach.transport = fail
    with pytest.raises(GuadeError):
        run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    assert ledger.counters["coach_calls"] == 1
    assert ledger.counters["coach_total_tokens"] > 1024
    assert ledger.counters["wonder_dispatches"] == 0


def test_unexpected_usage_blocks_further_calls(tmp_path):
    ledger = Ledger(tmp_path, Authorization())
    coach = Coach(ledger, api_key="fake", transport=lambda *a: {
        "usage": {"total_tokens": 99999}, "choices": []})
    with pytest.raises(BudgetExhausted):
        coach.decide("assignment", "x")
    assert ledger.counters["coach_total_tokens"] == 99999
    with pytest.raises(BudgetExhausted):
        coach.decide("assignment", "x")
    assert ledger.counters["coach_calls"] == 1


def test_stop_between_coach_and_engineer_prevents_dispatch(tmp_path):
    ledger, task, coach, dispatcher, _ = setup_cycle(tmp_path)
    transport = coach.transport
    def request_stop(*a):
        response = transport(*a)
        (ledger.state_dir / "STOP").write_text("stop")
        return response
    coach.transport = request_stop
    with pytest.raises(GuadeError):
        run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    assert ledger.counters["wonder_dispatches"] == 0
    assert not (tmp_path / "guade" / "mission.lock").exists()


def test_interruption_recorded_and_not_automatically_retried(tmp_path):
    def interrupted(*a):
        raise KeyboardInterrupt()
    ledger, task, coach, dispatcher, calls = setup_cycle(tmp_path, dispatch=interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    stored = json.loads(ledger.path.read_text())
    assert stored["tasks"]["bounded"]["status"] == "interrupted"
    assert stored["counters"]["wonder_dispatches"] == 1
    assert not (tmp_path / "guade" / "mission.lock").exists()
    with pytest.raises(GuadeError):
        run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    assert len(calls) == 1


def test_workspace_lock_prevents_second_cycle(tmp_path):
    ledger, task, coach, dispatcher, calls = setup_cycle(tmp_path)
    lock = MissionLock(tmp_path / "guade")
    lock.acquire()
    try:
        with pytest.raises(MissionError):
            run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    finally:
        lock.release()
    assert not calls


def test_monotonic_deadline_survives_clock_rollback(tmp_path, monkeypatch):
    clock = [1000.0, 500.0]
    monkeypatch.setattr("guade.mission.time.time", lambda: clock[0])
    monkeypatch.setattr("guade.mission.time.monotonic", lambda: clock[1])
    mission = Mission(tmp_path, 10)
    clock[:] = [900.0, 511.0]
    with pytest.raises(MissionError):
        mission.check_allowed("dispatch")


def test_check_is_read_only_and_cli_requires_separate_live_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "fake")
    state = tmp_path / "never-created"
    assert cli.main(["--state-dir", str(state), "check"]) == 0
    assert not state.exists()
    assert cli.main(["--state-dir", str(state), "run-task", "--tasks", "missing.json"]) == 2
    assert not state.exists()


@pytest.mark.parametrize("value", [-1, 0, True, float("nan")])
def test_invalid_timeouts_refused(value):
    with pytest.raises(GuadeError):
        Authorization.from_dict({"dispatch_timeout_seconds": value})


def test_live_provider_requires_locked_mission(tmp_path):
    ledger = Ledger(tmp_path, Authorization())
    coach = Coach(ledger, api_key="fake")
    with pytest.raises(GuadeError, match="active, locked"):
        coach.decide("assignment", "must not transmit")
    assert ledger.counters["coach_calls"] == 0


def test_control_tamper_stops_before_review(tmp_path):
    def engineer(cmd, timeout, cwd):
        (cwd / "state" / "audit.jsonl").write_text("erased")
        return passing_tests(cmd, timeout, cwd)
    ledger, task, coach, dispatcher, calls = setup_cycle(tmp_path, dispatch=engineer)
    with pytest.raises(GuadeError):
        run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    assert len(calls) == 1


def test_planning_exposure_limit_is_enforced_but_not_billing(tmp_path):
    ledger = Ledger(tmp_path, Authorization(baseten_budget_usd_cap=0.000001))
    coach = Coach(ledger, api_key="fake", transport=lambda *a: pytest.fail("no call"))
    with pytest.raises(BudgetExhausted, match="not a provider billing cap"):
        coach.decide("assignment", "x")
    assert ledger.counters["coach_calls"] == 0


def test_allowlist_refuses_escape_and_runtime_controls(tmp_path):
    from guade.boundaries import allowed_paths
    for path in ("../elsewhere", "guade/runtime.py", ".env", "C:/other.txt", ".git/config"):
        with pytest.raises(GuadeError):
            allowed_paths(tmp_path, [path])


def test_scope_tracks_deletion_and_preserves_unrelated_baseline(tmp_path):
    from guade.boundaries import WorkspaceBoundary
    (tmp_path / "keep.txt").write_text("unrelated existing work")
    (tmp_path / "result.md").write_text("old contents\n")
    boundary = WorkspaceBoundary(tmp_path, ["result.md"], tmp_path / "state")
    (tmp_path / "result.md").unlink()
    evidence = boundary.evidence()
    assert evidence["changed_files"] == ["result.md"]
    assert "-old contents" in evidence["diff"]
    assert (tmp_path / "keep.txt").read_text() == "unrelated existing work"


def test_large_readme_with_small_diff_is_supported(tmp_path):
    from guade.boundaries import WorkspaceBoundary
    doc = tmp_path / "README.md"
    doc.write_text("unchanged context\n" * 5000)
    boundary = WorkspaceBoundary(tmp_path, ["README.md"], tmp_path / "state")
    with doc.open("a") as handle:
        handle.write("new instructions\n")
    assert "+new instructions" in boundary.evidence()["diff"]


def test_oversized_diff_requires_owner_review(tmp_path):
    from guade.boundaries import WorkspaceBoundary
    boundary = WorkspaceBoundary(tmp_path, ["result.md"], tmp_path / "state")
    (tmp_path / "result.md").write_text("large change" * 5000)
    with pytest.raises(GuadeError, match="complete diff exceeds"):
        boundary.evidence()


def test_windows_subprocess_cleanup_on_stop(tmp_path, monkeypatch):
    import subprocess
    calls = []
    class Process:
        pid = 12345
        def communicate(self, timeout):
            return ("", "")
    class Guard:
        def check_allowed(self, action):
            raise MissionError("STOP")
    monkeypatch.setattr("guade.runtime.subprocess.Popen", lambda *a, **kw: Process())
    monkeypatch.setattr("guade.runtime.subprocess.run", lambda cmd, **kw:
                        calls.append(cmd) or SimpleNamespace(returncode=0))
    monkeypatch.setattr("guade.runtime.os.name", "nt")
    with pytest.raises(MissionError):
        _subprocess_runner(["fake"], 10, tmp_path, Guard())
    assert calls == [["taskkill", "/PID", "12345", "/T", "/F"]]


def test_uncertain_cleanup_retains_workspace_lock(tmp_path):
    def uncertain(*a):
        raise ProcessUncertain("cannot confirm termination")
    ledger, task, coach, dispatcher, _ = setup_cycle(tmp_path, dispatch=uncertain)
    with pytest.raises(GuadeError):
        run_cycle(task, coach, dispatcher, ledger, tmp_path, passing_tests)
    assert (tmp_path / "guade" / "mission.lock").exists()


def test_campaign_uses_finite_owner_tasks_and_stops_on_failure(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_offline.py").write_text("def test_ok(): pass")
    monkeypatch.setattr(cli, "ROOT", root)
    config = root / "grant.json"
    config.write_text(json.dumps({"max_wonder_dispatches": 1, "max_correction_retries": 0}))
    tasks = root / "tasks.json"
    tasks.write_text(json.dumps([
        {"id": name, "title": name, "prompt": "bounded", "allowed_files": ["result.md"],
         "tests": ["tests/test_offline.py"]} for name in ("one", "two")]))
    seen = []
    def fake_cycle(task, coach, dispatcher, ledger, workspace, **kw):
        assert kw["_mission_lock"].acquired
        assert (root / "guade" / "mission.lock").exists()
        seen.append(task["id"])
        return {"status": "failed"}
    monkeypatch.setattr(cli, "run_cycle", fake_cycle)
    result = cli.main(["--state-dir", str(root / "guade" / "state-campaign"),
                       "--config", str(config), "run-campaign", "--tasks", str(tasks),
                       "--approved-live"])
    assert result == 1 and seen == ["one"]
    assert not (root / "guade" / "mission.lock").exists()


def test_cli_refuses_historical_activation_writes(monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", "fake")
    assert cli.main(["run-demo", "--approved-live"]) == 2


@pytest.mark.parametrize("choices", [[], [None], [{}, {}]])
def test_malformed_or_ambiguous_provider_choices_never_approve(tmp_path, choices):
    ledger = Ledger(tmp_path, Authorization())
    coach = Coach(ledger, api_key="fake", transport=lambda *a: {"choices": choices})
    with pytest.raises(CoachUnavailable):
        coach.decide("assignment", "x")
    assert ledger.counters["coach_total_tokens"] > 1024


def test_real_windows_child_timeout_cleanup(tmp_path):
    """A harmless local Python sleeper exercises real Windows tree cleanup."""
    import os
    import subprocess
    import sys
    if os.name != "nt":
        pytest.skip("Windows process-tree integration check")
    pid_file = tmp_path / "child.pid"
    code = ("import os,time,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)")
    with pytest.raises(subprocess.TimeoutExpired):
        _subprocess_runner([sys.executable, "-B", "-c", code, str(pid_file)], 1.0, tmp_path)
    pid = pid_file.read_text()
    query = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                           capture_output=True, text=True, timeout=10)
    assert query.returncode == 0
    assert f'","{pid}",' not in query.stdout


def test_muse_terminal_and_usage_retained_without_credentials(monkeypatch):
    from guade.runtime import parse_jsonl
    monkeypatch.setenv("BASETEN_API_KEY", "private-fixture-key")
    text = '\n'.join(json.dumps({"payload": item}) for item in [
        {"kind": "run_usage", "models": [{"model": "wonder", "input_tokens": 50, "calls": 2}]},
        {"kind": "run_terminal", "terminal": "failed", "reason": "step_limit",
         "text": "Incomplete. private-fixture-key"},
    ])
    parsed = parse_jsonl(text)
    assert parsed["terminal"] == "failed" and parsed["terminal_reason"] == "step_limit"
    assert parsed["model_usage"][0]["input_tokens"] == 50
    assert "Incomplete" in parsed["engineer_report"]
    assert "private-fixture-key" not in parsed["engineer_report"]
