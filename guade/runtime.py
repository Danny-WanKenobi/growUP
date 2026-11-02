"""GUADE: minimal development-only autonomous engineering runtime.

Loop: Baseten Muse Spark Open (Super-Coach) assigns/reviews -> Muse Wonder
(headless `muse exec`) engineers -> deterministic tests validate -> bounded
correction. The owner authorizes the envelope; this runtime enforces it
fail-closed and never grants itself new authority.

Development only. Not imported by the Grow Up application. No production
access, no deployment, no Git writes. State lives in a local ledger plus an
append-only audit log (JSONL). Standard library only.

Budget model: counters are reserved BEFORE any provider call, so a crash
after reservation still consumes the allowance (the Census claim pattern).
The Baseten dollar cap is enforced conservatively through coach call and
token ceilings; the runtime records actual usage from API responses but
cannot guarantee a dollar figure without provider pricing.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .credentials import resolve_baseten_api_key

COACH_MODEL = "meta-models/muse-spark-open-1026"
COACH_URL = "https://inference.baseten.co/v1/chat/completions"
ENGINEER_MODEL = "muse-wonder-1.0"
DECISIONS = ("APPROVE", "REVISE", "STOP")


class GuadeError(Exception):
    """Base GUADE failure."""


class BudgetExhausted(GuadeError):
    """The authorized envelope is exhausted; refuse before any provider call."""


class CoachUnavailable(GuadeError):
    """The Super-Coach cannot be called (missing credential or API failure)."""


_AUTHORIZATION_FIELDS = {
    "max_wonder_dispatches", "max_steps_per_task", "max_correction_retries",
    "max_coach_calls", "max_coach_total_tokens", "coach_max_tokens_per_call",
    "baseten_budget_usd_cap", "dispatch_timeout_seconds", "coach_timeout_seconds",
}


@dataclass(frozen=True)
class Authorization:
    """Owner-granted envelope. Defaults mirror the 2026-11-02 trial grant."""

    max_wonder_dispatches: int = 3
    max_steps_per_task: int = 20
    max_correction_retries: int = 1
    max_coach_calls: int = 6
    max_coach_total_tokens: int = 60_000
    coach_max_tokens_per_call: int = 1_024
    baseten_budget_usd_cap: float = 10.0
    dispatch_timeout_seconds: int = 900
    coach_timeout_seconds: int = 60

    @classmethod
    def from_dict(cls, data: dict) -> "Authorization":
        unknown = set(data) - _AUTHORIZATION_FIELDS - {"comment"}
        if unknown:
            raise GuadeError(f"unknown authorization fields: {sorted(unknown)}")
        values = {k: v for k, v in data.items() if k in _AUTHORIZATION_FIELDS}
        auth = cls(**values)
        if min(auth.max_wonder_dispatches, auth.max_steps_per_task,
               auth.max_coach_calls, auth.max_coach_total_tokens) < 1:
            raise GuadeError("authorization limits must be positive")
        if auth.max_correction_retries < 0 or auth.baseten_budget_usd_cap <= 0:
            raise GuadeError("invalid authorization limits")
        return auth


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Ledger:
    """Persistent task/budget state with an append-only audit log."""

    def __init__(self, state_dir: Path, authorization: Authorization) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.authorization = authorization
        self.path = self.state_dir / "ledger.json"
        self.audit_path = self.state_dir / "audit.jsonl"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {
                "counters": {"wonder_dispatches": 0, "coach_calls": 0,
                             "coach_total_tokens": 0, "correction_retries": 0},
                "tasks": {},
            }
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def audit(self, event: str, **fields: object) -> None:
        record = {"ts": _utc_now(), "event": event, **fields}
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    @property
    def counters(self) -> dict:
        return self.data["counters"]

    def reserve_dispatch(self) -> None:
        counters = self.counters
        if counters["wonder_dispatches"] >= self.authorization.max_wonder_dispatches:
            raise BudgetExhausted("Muse Wonder dispatch allowance exhausted")
        counters["wonder_dispatches"] += 1
        self._save()
        self.audit("dispatch_reserved", count=counters["wonder_dispatches"])

    def reserve_retry(self) -> None:
        counters = self.counters
        if counters["correction_retries"] >= self.authorization.max_correction_retries:
            raise BudgetExhausted("correction retry allowance exhausted")
        counters["correction_retries"] += 1
        self._save()
        self.audit("retry_reserved", count=counters["correction_retries"])

    def reserve_coach_call(self) -> None:
        counters = self.counters
        auth = self.authorization
        if counters["coach_calls"] >= auth.max_coach_calls:
            raise BudgetExhausted("Super-Coach call allowance exhausted")
        if counters["coach_total_tokens"] + auth.coach_max_tokens_per_call \
                > auth.max_coach_total_tokens:
            raise BudgetExhausted("Super-Coach token ceiling would be exceeded")
        counters["coach_calls"] += 1
        counters["coach_total_tokens"] += auth.coach_max_tokens_per_call
        self._save()
        self.audit("coach_call_reserved", count=counters["coach_calls"])

    def reconcile_coach_tokens(self, actual_total_tokens: int) -> None:
        reserved = self.authorization.coach_max_tokens_per_call
        self.counters["coach_total_tokens"] += actual_total_tokens - reserved
        self._save()
        self.audit("coach_tokens_reconciled", actual=actual_total_tokens)

    def set_task(self, task_id: str, **fields: object) -> None:
        task = self.data["tasks"].setdefault(task_id, {})
        task.update(fields)
        task["updated_at"] = _utc_now()
        self._save()


def _urllib_transport(payload: dict, api_key: str, timeout: float) -> dict:
    request = urllib.request.Request(
        COACH_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise CoachUnavailable(f"coach API HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CoachUnavailable(f"coach API unreachable: {type(exc).__name__}") from exc


def _response_text(choice: dict) -> str:
    """Extract the explicit final answer from an OpenAI-compatible choice.

    Only final-answer fields count: string ``content``, structured
    content parts, or completions-style ``text``. Reasoning fields are
    diagnostics only and are never returned here, so internal analysis
    cannot authorize anything. Returns "" when no final answer exists
    (e.g. a tool-call-only message).
    """
    message = choice.get("message")
    if not isinstance(message, dict):
        message = {}
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        joined = "".join(
            part["text"] for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str))
        if joined.strip():
            return joined
    text = choice.get("text")
    return text if isinstance(text, str) else ""


def _reasoning_present(choice: dict) -> bool:
    message = choice.get("message")
    if not isinstance(message, dict):
        return False
    return any(isinstance(message.get(field), str)
               and bool(message[field].strip())
               for field in ("reasoning_content", "reasoning"))


def _decision_from_text(text: str) -> str:
    """Exactly one distinct decision word wins; anything else is STOP."""
    found = set(re.findall(r"\b(?:" + "|".join(DECISIONS) + r")\b",
                           text.upper()))
    return found.pop() if len(found) == 1 else "STOP"


class Coach:
    """Baseten Muse Spark Open supervisor adapter (fail-closed)."""

    def __init__(self, ledger: Ledger, transport=None, api_key: str | None = None) -> None:
        self.ledger = ledger
        self.transport = transport or _urllib_transport
        self.api_key = resolve_baseten_api_key().value if api_key is None else api_key

    def available(self) -> bool:
        return bool(self.api_key.strip())

    def decide(self, kind: str, prompt: str) -> dict:
        if not self.available():
            raise CoachUnavailable(
                "BASETEN_API_KEY is not provisioned: not in the process "
                "environment, the Windows user environment, or a "
                "restricted GUADE env file (see guade.credentials)")
        auth = self.ledger.authorization
        self.ledger.reserve_coach_call()
        payload = {
            "model": COACH_MODEL,
            "max_tokens": auth.coach_max_tokens_per_call,
            "messages": [
                {"role": "system", "content": (
                    "You are the GUADE Super-Coach supervising a bounded, "
                    "development-only engineering task. Reply with exactly "
                    "one line whose first word is APPROVE, REVISE, or STOP, "
                    "optionally followed by ' - ' and a brief reason. "
                    "Output no other text. You cannot authorize spending, "
                    "deployment, or scope beyond the envelope.")},
                {"role": "user", "content": prompt},
            ],
        }
        response = self.transport(payload, self.api_key,
                                  float(auth.coach_timeout_seconds))
        try:
            choice = response["choices"][0]
            if not isinstance(choice, dict):
                raise TypeError("choice is not an object")
        except (KeyError, IndexError, TypeError) as exc:
            raise CoachUnavailable("malformed coach response") from exc
        content = _response_text(choice)
        finish_reason = choice.get("finish_reason")
        exhausted = finish_reason == "length"
        usage = response.get("usage") or {}
        actual = int(usage.get("total_tokens") or max(1, len(content) // 4))
        self.ledger.reconcile_coach_tokens(actual)
        decision = "STOP" if exhausted else _decision_from_text(content)
        self.ledger.audit(
            "coach_decision", kind=kind, decision=decision,
            usage_tokens=actual,
            usable=bool(content.strip()) and not exhausted,
            finish_reason=finish_reason, exhausted=exhausted,
            reasoning_present=_reasoning_present(choice))
        return {"decision": decision, "text": content,
                "usage_tokens": actual}


def parse_jsonl(stdout: str) -> dict:
    """Tolerantly parse `muse exec --json` output."""
    events, kinds, session_id = 0, [], None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        events += 1
        payload = record.get("payload") or {}
        kind = payload.get("kind") or record.get("payload_type") or "unknown"
        kinds.append(kind)
        stream = record.get("stream") or {}
        if stream.get("kind") == "session":
            session_id = stream.get("id") or session_id
    return {"events": events, "kinds": kinds, "session_id": session_id}


def _subprocess_runner(cmd: list[str], timeout: float, cwd: Path):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, cwd=str(cwd))


def muse_command(args: list[str]) -> list[str]:
    """Resolve the `muse` CLI, wrapping the Windows .cmd shim if needed."""
    exe = shutil.which("muse")
    if exe is None:
        raise GuadeError("muse CLI is not on PATH")
    if exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *args]
    return [exe, *args]


class Dispatcher:
    """Headless Muse Wonder engineering dispatcher via `muse exec`."""

    def __init__(self, ledger: Ledger, runner=None) -> None:
        self.ledger = ledger
        self._default_runner = runner is None
        self.runner = runner or _subprocess_runner

    def dispatch(self, prompt_file: Path, workspace: Path,
                 provider: str = "meta") -> dict:
        if provider not in ("meta", "echo"):
            raise GuadeError(f"unsupported provider: {provider}")
        auth = self.ledger.authorization
        if provider == "meta":
            self.ledger.reserve_dispatch()
        args = ["exec", "--provider", provider]
        if provider == "meta":
            args += ["--model", ENGINEER_MODEL]
        args += ["--json",
                 "--max-model-steps", str(auth.max_steps_per_task),
                 "--prompt-file", str(prompt_file),
                 "--workspace", str(workspace), "--disable-shell"]
        cmd = muse_command(args) if self._default_runner else ["muse", *args]
        result = self.runner(cmd, float(auth.dispatch_timeout_seconds),
                             Path(workspace))
        parsed = parse_jsonl(result.stdout or "")
        outcome = {"returncode": result.returncode, **parsed,
                   "stderr_tail": (result.stderr or "")[-500:]}
        self.ledger.audit("dispatch_finished", provider=provider, **{
            k: outcome[k] for k in ("returncode", "events", "session_id")})
        return outcome


def run_tests(command: list[str], workspace: Path, runner=None) -> dict:
    runner = runner or _subprocess_runner
    result = runner(command, 1200.0, Path(workspace))
    tail = ((result.stdout or "") + (result.stderr or "")).strip()[-2000:]
    return {"returncode": result.returncode, "passed": result.returncode == 0,
            "tail": tail}


def run_cycle(task: dict, coach: Coach, dispatcher: Dispatcher,
              ledger: Ledger, workspace: Path, test_runner=None) -> dict:
    """One bounded Super-Coach -> Wonder -> tests -> review cycle."""
    task_id = task["id"]
    ledger.set_task(task_id, status="running", title=task.get("title", ""))
    ledger.audit("cycle_started", task=task_id)
    assignment = coach.decide(
        "assignment",
        f"Task {task_id}: {task.get('title', '')}\n\n{task['prompt']}\n\n"
        "Confirm this is a safe, bounded, development-only task.")
    if assignment["decision"] == "STOP":
        ledger.set_task(task_id, status="blocked_coach_stop")
        ledger.audit("cycle_finished", task=task_id, status="blocked_coach_stop")
        return {"status": "blocked_coach_stop", "task": task_id}

    prompt_file = Path(task["prompt_file"])
    feedback = ""
    retries_used = 0
    while True:
        if feedback:
            ledger.reserve_retry()
            retries_used += 1
            retry_file = prompt_file.with_suffix(".retry.md")
            retry_file.write_text(
                task["prompt"] + "\n\nCorrection feedback:\n" + feedback,
                encoding="utf-8")
            prompt_file = retry_file
        dispatch = dispatcher.dispatch(prompt_file, workspace)
        tests = run_tests(task["test_command"], workspace, runner=test_runner)
        verified = tests["passed"] and dispatch["returncode"] == 0
        detail = tests["tail"]
        if task.get("verify") is not None:
            ok, verify_detail = task["verify"]()
            verified = verified and ok
            detail += "\n" + verify_detail
        ledger.audit("validation_finished", task=task_id, passed=verified)
        review = coach.decide(
            "review",
            f"Task {task_id} attempt {retries_used + 1}. "
            f"Dispatch exit={dispatch['returncode']} events={dispatch['events']}. "
            f"Deterministic validation passed={verified}.\n{detail}\n"
            "APPROVE only if the evidence supports completion.")
        if verified and review["decision"] == "APPROVE":
            status = "completed"
        elif review["decision"] == "STOP":
            status = "stopped_by_coach"
        elif retries_used < ledger.authorization.max_correction_retries \
                and ledger.counters["correction_retries"] \
                < ledger.authorization.max_correction_retries:
            feedback = review["text"] + "\n" + detail
            ledger.audit("correction_planned", task=task_id,
                         attempt=retries_used + 1)
            continue
        else:
            status = "failed"
        ledger.set_task(task_id, status=status, retries=retries_used)
        ledger.audit("cycle_finished", task=task_id, status=status)
        return {"status": status, "task": task_id, "retries": retries_used,
                "dispatch": dispatch, "tests_passed": tests["passed"]}
