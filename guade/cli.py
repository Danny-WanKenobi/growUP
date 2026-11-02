"""GUADE command line: check | smoke-echo | run-demo.

Development only. `run-demo` is the single authorized live trial task
(documentation-only); it fails closed before any provider call when the
Super-Coach credential is absent. No Git writes, no deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

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


def _ledger(args) -> Ledger:
    config_path = Path(args.config) if args.config else \
        Path(__file__).with_name("config.example.json")
    auth = Authorization.from_dict(
        json.loads(config_path.read_text(encoding="utf-8")))
    return Ledger(Path(args.state_dir), auth)


def cmd_check(args) -> int:
    ledger = _ledger(args)
    coach = Coach(ledger)
    print(f"coach model: meta-models/muse-spark-open-1026")
    print(f"BASETEN_API_KEY: {'SET' if coach.available() else 'ABSENT'}")
    print(f"engineer: muse exec --provider meta --model muse-wonder-1.0")
    print(f"authorization: {ledger.authorization}")
    print(f"counters: {ledger.counters}")
    if not coach.available():
        print("BLOCKED: provision BASETEN_API_KEY (development-only secret) "
              "before any live cycle. No provider call was made.")
        return 2
    print("READY: live cycle permitted within the authorized envelope.")
    return 0


def cmd_smoke_echo(args) -> int:
    ledger = _ledger(args)
    prompt = ledger.state_dir / "smoke-prompt.md"
    prompt.write_text("Reply with the single word READY", encoding="utf-8")
    result = Dispatcher(ledger).dispatch(prompt, ROOT, provider="echo")
    ledger.audit("smoke_echo", **{k: result[k] for k in
                                  ("returncode", "events", "session_id")})
    print(json.dumps(result, indent=2))
    return 0 if result["events"] > 0 else 1


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
    }
    try:
        result = run_cycle(task, coach, Dispatcher(ledger), ledger, ROOT)
    except (CoachUnavailable, GuadeError) as exc:
        print(f"BLOCKED/FAILED: {exc}")
        return 2
    print(json.dumps({"result": result, "counters": ledger.counters},
                     indent=2, default=str))
    return 0 if result["status"] == "completed" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="guade")
    parser.add_argument("--state-dir", default=str(ROOT / "guade" / "state"))
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("smoke-echo")
    sub.add_parser("run-demo")
    args = parser.parse_args(argv)
    handlers = {"check": cmd_check, "smoke-echo": cmd_smoke_echo,
                "run-demo": cmd_run_demo}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
