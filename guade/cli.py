"""Windows development CLI: readiness, retained demo and approved task lists.

Development only. `run-demo` is the single authorized live trial task
(documentation-only); it fails closed before any provider call when the
Super-Coach credential is absent. No Git writes, no deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from .credentials import credential_status, missing_key_guidance
from .runtime import (Authorization, Coach, CoachUnavailable, Dispatcher,
                      GuadeError, Ledger, run_cycle)

ROOT = Path(__file__).resolve().parent.parent
DEMO_DOC = ROOT / "documentation" / "guade-activation-trial.md"
DEMO_MARKERS = ("GUADE", "P1", "SearXNG", "Brave", "Qwen")

DEMO_PROMPT = """Create exactly one new documentation file at
documentation/guade-activation-trial.md and change nothing else.

The file must be a short Markdown note titled "GUADE activation trial" that
states: GUADE is development-only; Gabriel discovery precedence is P1 free
official government data, then P2 SearXNG, then P3 Brave Search API as a
paid fallback requiring standing authorization; Qwen is excluded from
Gabriel; Muse Spark Open supervises, Muse Wonder engineers, deterministic
tests validate, and the owner rules on milestones.

Do not edit any other file. Do not run git commands. Do not access the
network, any database, or production systems.
"""


def _ledger(args, *, read_only=False) -> Ledger:
    config_path = Path(args.config) if args.config else \
        Path(__file__).with_name("config.example.json")
    auth = Authorization.from_dict(
        json.loads(config_path.read_text(encoding="utf-8")))
    state_dir = Path(args.state_dir).resolve()
    if not read_only:
        if (not state_dir.is_relative_to(ROOT / "guade")
                or state_dir.name in {"state", "state-operating", "state-overnight-20261102"}
                or not state_dir.name.startswith("state-")):
            raise GuadeError("use a new guade/state-* directory; historical ledgers are retained read-only")
    return Ledger(state_dir, auth, read_only=read_only)


def cmd_check(args) -> int:
    ledger = _ledger(args, read_only=True)
    coach = Coach(ledger)
    status = credential_status()
    auth = ledger.authorization
    counters = ledger.counters
    remaining = {
        "wonder_dispatches": auth.max_wonder_dispatches
        - counters["wonder_dispatches"],
        "coach_calls": auth.max_coach_calls - counters["coach_calls"],
        "coach_tokens": auth.max_coach_total_tokens
        - counters["coach_total_tokens"],
        "correction_retries": auth.max_correction_retries
        - counters["correction_retries"],
    }
    print(f"coach model: meta-models/muse-spark-open-1026")
    print(f"BASETEN_API_KEY: {'SET' if coach.available() else 'ABSENT'}"
          f" (source: {status['source']})")
    for problem in status["problems"]:
        print(f"credential warning: {problem}")
    print(f"engineer: muse exec --provider meta --model muse-wonder-1.0")
    print(f"authorization: {ledger.authorization}")
    print(f"counters: {ledger.counters}")
    print(f"remaining: {remaining}")
    if not coach.available():
        print("BLOCKED: " + missing_key_guidance()
              + " No provider call was made.")
        return 2
    if (remaining["wonder_dispatches"] <= 0
            or remaining["coach_calls"] <= 0
            or remaining["coach_tokens"] < auth.coach_max_tokens_per_call):
        print("BLOCKED: this ledger's authorized envelope is exhausted "
              "(see remaining above). Do not reset or reuse it; a live "
              "cycle needs a separate owner-approved ledger. No "
              "provider call was made.")
        return 2
    print("CONFIGURED: credential present and ledger has headroom. "
          "Not authentication, scope verification, or spending authorization.")
    return 0


def cmd_smoke_echo(args) -> int:
    from .mission import Mission, MissionLock
    lock = MissionLock(ROOT / "guade")
    lock.acquire()
    try:
        ledger = _ledger(args)
        ledger.guard = Mission(ledger.state_dir, ledger.authorization.max_runtime_seconds)
        ledger.check_allowed("echo smoke")
        prompt = ledger.state_dir / "smoke-prompt.md"
        prompt.write_text("Reply with the single word READY", encoding="utf-8")
        result = Dispatcher(ledger).dispatch(prompt, ROOT, provider="echo")
        ledger.audit("smoke_echo", **{k: result[k] for k in
                                      ("returncode", "events", "session_id")})
        print(json.dumps(result, indent=2))
        return 0 if result["events"] > 0 else 1
    finally:
        lock.release()


def _demo_doc_hash():
    if not DEMO_DOC.exists():
        return None
    return hashlib.sha256(DEMO_DOC.read_bytes()).hexdigest()


def _verify_demo_doc(before_hash):
    """Evidence = required markers AND content changed by this dispatch."""
    def verify():
        if not DEMO_DOC.exists():
            return False, "demo document missing"
        text = DEMO_DOC.read_text(encoding="utf-8")
        missing = [m for m in DEMO_MARKERS if m not in text]
        if missing:
            return False, f"missing markers: {missing}"
        if _demo_doc_hash() == before_hash:
            return False, "demo document unchanged by this dispatch"
        return True, "demo document created/modified with required markers"
    return verify


def cmd_run_demo(args) -> int:
    if not args.approved_live:
        raise GuadeError("separate owner spending approval and --approved-live required")
    ledger = _ledger(args)
    coach = Coach(ledger)
    if not coach.available():
        print("BLOCKED: BASETEN_API_KEY absent; no provider call made, "
              "no dispatch consumed.")
        return 2
    prompt_file = ledger.state_dir / "demo-prompt.md"
    prompt_file.write_text(DEMO_PROMPT, encoding="utf-8")
    task = {
        "id": "guade-trial-001",
        "title": "Harmless documentation-only GUADE activation trial",
        "prompt": DEMO_PROMPT,
        "prompt_file": str(prompt_file),
        "test_command": [sys.executable, "-m", "pytest",
                         "tests/test_discovery_precedence.py", "-q"],
        "verify": _verify_demo_doc(_demo_doc_hash()),
        "allowed_files": ["documentation/guade-activation-trial.md"],
    }
    try:
        result = run_cycle(task, coach, Dispatcher(ledger), ledger, ROOT)
    except (CoachUnavailable, GuadeError) as exc:
        print(f"BLOCKED/FAILED: {exc}")
        return 2
    print(json.dumps({"result": result, "counters": ledger.counters},
                     indent=2, default=str))
    return 0 if result["status"] == "completed" else 1


def load_tasks(path: Path) -> list[dict]:
    """Explicit owner-reviewed task list; no model-generated tasks or commands."""
    from .boundaries import allowed_paths
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise GuadeError("task file must contain a nonempty JSON list")
    ids = set()
    for task in raw:
        if not isinstance(task, dict) or set(task) != {"id", "title", "prompt", "allowed_files", "tests"}:
            raise GuadeError("each task requires exactly id, title, prompt, allowed_files, tests")
        for field in ("id", "title", "prompt"):
            if not isinstance(task[field], str) or not task[field].strip():
                raise GuadeError(f"task {field} must be nonempty text")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task["id"]) or task["id"] in ids:
            raise GuadeError("task ids must be unique safe identifiers")
        ids.add(task["id"])
        allowed_paths(ROOT, task["allowed_files"])
        if not isinstance(task["tests"], list) or not task["tests"]:
            raise GuadeError("task requires focused test file paths")
        for value in task["tests"]:
            if not isinstance(value, str) or not value.startswith("tests/") or not value.endswith(".py"):
                raise GuadeError("tests must name repository Python test files, not shell commands")
            target = (ROOT / value).resolve()
            if not target.is_relative_to(ROOT / "tests") or not target.is_file():
                raise GuadeError("test file is missing or outside tests/")
    return raw


def cmd_run_tasks(args) -> int:
    from .mission import write_authorization_snapshot
    if not args.approved_live:
        raise GuadeError("separate owner spending approval and --approved-live required")
    tasks = load_tasks(Path(args.tasks))
    if args.command == "run-task" and len(tasks) != 1:
        raise GuadeError("run-task requires exactly one task")
    from .mission import MissionLock
    campaign_lock = MissionLock(ROOT / "guade")
    campaign_lock.acquire()
    try:
        ledger = _ledger(args)
        manifest = ledger.state_dir / "tasks.json"
        serialized = json.dumps(tasks, sort_keys=True, indent=2)
        if manifest.exists() and manifest.read_text(encoding="utf-8") != serialized:
            raise GuadeError("ledger task list differs; owner reconciliation required")
        manifest.write_text(serialized, encoding="utf-8")
        write_authorization_snapshot(ledger.state_dir, asdict(ledger.authorization))
        for item in tasks:
            prompt_file = ledger.state_dir / (item["id"] + ".md")
            prompt_file.write_text(
                item["prompt"] + "\n\nAllowed files (exact paths):\n"
                + "\n".join(item["allowed_files"])
                + "\nDo not edit other files, runtime controls or secrets. Do not use Git, "
                "network, databases, deployments or external actions.\n", encoding="utf-8")
            task = {**item, "prompt_file": str(prompt_file),
                    "prompt": prompt_file.read_text(encoding="utf-8"),
                    "test_command": [sys.executable, "-m", "pytest", "-q",
                                     "-p", "no:cacheprovider", *item["tests"]]}
            result = run_cycle(task, Coach(ledger), Dispatcher(ledger), ledger, ROOT,
                               _mission_lock=campaign_lock)
            print(json.dumps(result, default=str))
            if result["status"] != "completed":
                return 1
        return 0
    finally:
        campaign_lock.release()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="guade")
    parser.add_argument("--state-dir", default=str(ROOT / "guade" / "state"))
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("smoke-echo")
    sub.add_parser("run-demo")
    for name in ("run-task", "run-campaign"):
        child = sub.add_parser(name)
        child.add_argument("--tasks", required=True,
                           help="owner-reviewed JSON task list; campaign stops when list completes")
        child.add_argument("--approved-live", action="store_true")
    sub.choices["run-demo"].add_argument("--approved-live", action="store_true")
    args = parser.parse_args(argv)
    handlers = {"check": cmd_check, "smoke-echo": cmd_smoke_echo,
                "run-demo": cmd_run_demo, "run-task": cmd_run_tasks,
                "run-campaign": cmd_run_tasks}
    try:
        return handlers[args.command](args)
    except GuadeError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
