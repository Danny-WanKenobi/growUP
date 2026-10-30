"""Wondrue: minimal conversational interface over the MVP workflow.

Wondrue coordinates presentation of Gabriel/Raphael/Onesimus findings.
She never fabricates agent findings: every answer cites persisted
records or explicit unknowns, and every model output is validated
before use.

Server-side access modes (selected per request, enforced here):
- read-only: inspect/search/explain only. No business mutations, no
  live model calls (replay explanations only), no external calls.
- ask: consequential intents return a preview with concrete
  operations, inputs, effects, and cost bounds plus an approval
  token; nothing executes until the token is presented back.
- full: executes permitted intents within explicit scopes and
  limits. Paid inference still requires spending approval.

Model text can never become an executable instruction: intents and
proposals pass through strict allowlist validation, and only the
validated fields drive execution.
"""

from __future__ import annotations

import secrets
import time
from typing import Any
from uuid import UUID

from app.demo import DEMO_LABEL, DemoStore, run_demo_workflow
from app.model_adapter import (
    ModelError,
    ModelRequest,
    ReplayAdapter,
    spending_approved,
    validate_config_proposal,
    validate_intent,
)

MODES = ("read-only", "ask", "full")
TARGETS = ("real", "demo")

READ_ONLY_INTENTS = frozenset(
    {
        "inspect_candidate",
        "inspect_evidence",
        "explain_decision",
        "inspect_review",
        "help",
        "inspect_packages",
        "preview_simulation",
        "inspect_simulation",
        "inspect_discovery",
    }
)
CONSEQUENTIAL_INTENTS = frozenset(
    {
        "start_real_evaluation",
        "start_demo",
        "compare_scenarios",
        "stop",
        "run_simulation",
        "start_discovery",
        "stop_discovery",
        "resume_discovery",
    }
)
# Request-safety caps for Wondrue-driven discovery bursts; longer
# campaigns use repeated bursts or the runner CLI with the same bounds.
WONDRUE_BURST_MAX_PASSES = 5
WONDRUE_BURST_MAX_DURATION_SECONDS = 900.0
WONDRUE_BURST_MAX_EVALUATIONS = 5
# Worst-case metered calls per coordinator evaluation (mirrors the
# ``limits={"brave_attempts": 9}`` in start_evaluation_audited).
BRAVE_ATTEMPTS_PER_EVALUATION = 9

APPROVAL_TTL_SECONDS = 600


class WondrueError(ValueError):
    """Command rejected or failed (safe to report to the caller)."""


class WondrueAuthError(WondrueError):
    """Requested mode exceeds the caller's server-side grant."""


_GRANT_ORDER = {"read-only": 0, "ask": 1, "full": 2}


class ApprovalStore:
    """Short-lived server-side approval tokens (single-process MVP)."""

    def __init__(self) -> None:
        self._pending: dict[str, dict[str, Any]] = {}

    def issue(self, intent: str, args: dict[str, Any], target: str) -> str:
        token = secrets.token_urlsafe(24)
        self._pending[token] = {
            "intent": intent,
            "args": dict(args),
            "target": target,
            "expires": time.time() + APPROVAL_TTL_SECONDS,
        }
        return token

    def redeem(self, token: str) -> dict[str, Any]:
        entry = self._pending.pop(token, None)
        if entry is None or entry["expires"] < time.time():
            raise WondrueError("approval token invalid or expired")
        return entry


APPROVALS = ApprovalStore()
DEMO = DemoStore()


def _adapter(use_live: bool, mode: str) -> Any:
    if use_live:
        if mode == "read-only":
            raise WondrueError("read-only mode uses replay explanations, never live calls")
        if not spending_approved():
            raise WondrueError("live inference refused: spending not approved")
        from app.model_adapter import MetaLiveAdapter

        return MetaLiveAdapter()
    return ReplayAdapter(
        {
            "interpret_command": {"intent": "help", "args": {}},
            "propose_configs": {
                "options": [{"config": "bench"}, {"config": "rack3"}],
                "note": "replay: default two-configuration comparison",
            },
            "explain": {"text": "replay: explanation unavailable offline"},
        }
    )


def _complete(adapter: Any, request: ModelRequest, *, approved: bool) -> Any:
    if adapter.provider == "meta":
        return adapter.complete(request, approved=approved)
    return adapter.complete(request)


def interpret_command(text: str, *, use_live: bool, mode: str) -> tuple[dict[str, Any], Any]:
    """Interpret a command into a validated intent plus the model result."""
    adapter = _adapter(use_live, mode)
    result = _complete(adapter, ModelRequest("interpret_command", {"text": text}), approved=True)
    return validate_intent(result.result), result


def preview_intent(intent: str, args: dict[str, Any], target: str) -> dict[str, Any]:
    """Concrete preview for ask mode (operations, effects, cost bounds)."""
    if intent == "start_real_evaluation":
        return {
            "operations": ["brave acquisition (<= run bound)", "persist artifacts/dossier/qualification", "raphael authorize"],
            "inputs": {"assignment_id": args.get("assignment_id", ""), "query": args.get("query", "")},
            "effects": "writes workflow run + records; may spend Brave budget",
            "cost_bounds": {"brave_attempts": "<= run limit (default 9)", "meta_calls": "0 unless compare follows"},
        }
    if intent == "start_demo":
        return {
            "operations": ["run synthetic Gabriel/Raphael/Onesimus stages in demo store"],
            "inputs": {"run_key": args.get("run_key", "")},
            "effects": "demo-store writes only; no production records, no external calls",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": 0},
        }
    if intent == "compare_scenarios":
        return {
            "operations": ["model proposes options (catalog-validated)", "deterministic Onesimus math per option"],
            "inputs": {"target": target},
            "effects": "read-only comparison; may spend one model call",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": "<= 1"},
        }
    if intent == "stop":
        return {
            "operations": ["mark run stopped; records preserved"],
            "inputs": {"run": args.get("run", "")},
            "effects": "no further execution for that run",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": 0},
        }
    if intent == "run_simulation":
        return {
            "operations": [
                "freeze eligible Uriel packages",
                "write simulated delivery/response/conversion events",
            ],
            "inputs": {
                "cohort": args.get("cohort", ""),
                "seed": args.get("seed", ""),
                "package_entries": args.get("package_entries", ""),
                "idempotency_key": args.get("idempotency_key", ""),
            },
            "effects": "simulated rows only; no dossier, finance, actual-metric, or execution-state changes",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": 0},
        }
    if intent in ("start_discovery", "resume_discovery"):
        requested = _acquire_request(args)
        bounds = _burst_bounds(args)
        brave_bound = (
            bounds[0] * bounds[3] * BRAVE_ATTEMPTS_PER_EVALUATION
        )
        operations = ["bounded discovery burst (existing OSM adapter)"]
        if requested:
            operations.append(
                "coordinator evaluations of the backlog head "
                "(bounded evidence acquisition)"
            )
        effects = (
            "discovers real properties with checkpoints; stops on "
            "limits, Sabbath, or operator stop"
        )
        if requested:
            effects += (
                f"; drives up to {bounds[0] * bounds[3]} evaluations "
                "through metered Brave acquisition (operator grant "
                "required; unapproved requests run discovery only)"
            )
        else:
            effects += (
                "; coordinator evaluations are NOT run (acquire not "
                "requested): free OSM discovery only"
            )
        return {
            "operations": operations,
            "inputs": {
                "scope": args.get("scope", ""),
                "policy": args.get("policy", "pilot"),
                "max_passes": bounds[0],
                "max_duration_seconds": bounds[1],
                "max_created": bounds[2],
                "acquire": requested,
                "max_evaluations": bounds[3],
            },
            "effects": effects,
            "cost_bounds": {
                "brave_attempts": f"<= {brave_bound}" if requested else 0,
                "meta_calls": 0,
            },
        }
    if intent == "stop_discovery":
        return {
            "operations": ["request running worker to pause between passes"],
            "inputs": {"scope": args.get("scope", "")},
            "effects": "worker exits at the next pass boundary; checkpoints preserved",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": 0},
        }
    raise WondrueError(f"no preview for intent {intent!r}")


def _demo_run_key(args: dict[str, Any]) -> str:
    from app.demo import demo_id

    raw = str(args.get("run_key") or "demo-run-1")
    return raw if raw.startswith("demo-") else demo_id(raw)


def _synthetic_gate() -> None:
    """Refuse ordinary synthetic runs after an evidenced 144 pilot.

    Automated tests calling run_demo_workflow directly are unaffected;
    only operational selection through Wondrue is gated. Emergency
    diagnostic execution needs EMERGENCY_DIAGNOSTIC_DEMO=1.
    """
    import os

    from app.pilot import pilot_success_evidenced

    if os.getenv("EMERGENCY_DIAGNOSTIC_DEMO") == "1":
        return
    if pilot_success_evidenced():
        raise WondrueError(
            "pilot success is evidenced (144 qualified dossiers); synthetic "
            "upstream workflows are unavailable through ordinary selection. "
            "Emergency diagnostic execution requires owner authorization "
            "(EMERGENCY_DIAGNOSTIC_DEMO=1)."
        )


def _execute_demo(intent: str, args: dict[str, Any], store: DemoStore) -> dict[str, Any]:
    if intent == "start_demo":
        _synthetic_gate()
        summary = run_demo_workflow(store, _demo_run_key(args))
        return {"summary": summary, "label": DEMO_LABEL}
    if intent == "inspect_candidate":
        exported = store.export_run(_demo_run_key(args)) if _demo_run_key(args) in store.runs else None
        return {"run": exported, "label": DEMO_LABEL}
    if intent == "inspect_evidence":
        record = store.get_record(
            _demo_run_key(args), str(args.get("kind", "research_artifact")),
            str(args.get("key", "")),
        )
        return {"record": record, "label": DEMO_LABEL}
    if intent == "explain_decision":
        record = store.get_record(_demo_run_key(args), "raphael_decision", str(args.get("key", "")))
        if record is None:
            records = [
                r for (run, kind, _, _), r in store.records.items()
                if run == _demo_run_key(args) and kind == "raphael_decision"
            ]
            record = records[0] if records else None
        return {"decision": record["payload"] if record else None, "label": DEMO_LABEL}
    if intent == "stop":
        key = _demo_run_key(args)
        if key in store.runs:
            store.runs[key]["status"] = "stopped"
        return {"stopped": key, "label": DEMO_LABEL}
    if intent == "help":
        return {"help": _help_text(), "label": DEMO_LABEL}
    raise WondrueError(f"intent {intent!r} is not a demo intent")


def compare_with_proposal(
    proposal: dict[str, Any], site_inputs: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Deterministic comparison over a validated model proposal (pure).

    Each option runs Onesimus engineering first, then Michael's
    financial assessment over the same engineering outputs. Financial
    overrides apply only to Michael's bundle; engineering overrides
    apply only to the site/crop inputs.
    """
    from app.agents.onesimus.crops import crop_profile
    from app.agents.onesimus.service import (
        PARAMETER_CATALOG,
        ClassifiedInput,
        evaluate_scenario,
    )
    from app.michael import (
        FINANCIAL_CATALOG,
        FINANCIAL_NAMES,
        assess as michael_assess,
        financial_inputs_from_raw,
    )

    cleaned = validate_config_proposal(proposal)
    site_base: dict[str, ClassifiedInput] = {}
    for name, entry in (site_inputs or {}).items():
        if isinstance(entry, dict) and name in PARAMETER_CATALOG:
            site_base[name] = ClassifiedInput.model_validate({"name": name, **entry})
    compared = []
    for option in cleaned["options"]:
        crop = crop_profile()
        site = {name: item.model_copy() for name, item in site_base.items()}
        config = str(option.pop("config"))
        tenant = bool(option.pop("tenant_consolidation", True))
        engineering = {k: v for k, v in option.items() if k in PARAMETER_CATALOG}
        financial_raw = {
            **{
                k: v for k, v in (site_inputs or {}).items()
                if isinstance(v, dict) and k in FINANCIAL_NAMES
            },
            **{k: {"value": v} for k, v in option.items() if k in FINANCIAL_CATALOG},
        }
        for name, value in engineering.items():
            if name == "config":
                continue
            if name in site:
                site[name].value = float(value)
            elif name in crop and crop[name].value is not None:
                crop[name] = crop[name].model_copy(update={"value": float(value)})
        result = evaluate_scenario(config, crop, site, tenant_consolidation=tenant)
        finance = michael_assess(
            config,
            {
                "complete": result["complete"],
                "missing": result["missing"],
                "outputs": result["outputs"],
            },
            financial_inputs_from_raw(financial_raw, scenario=config),
        )
        compared.append(
            {
                "config": config,
                "complete": result["complete"],
                "outputs": result["outputs"],
                "financial_complete": finance["complete"],
                "financial_verdict": finance["verdict"],
                "financial_outputs": finance["outputs"],
            }
        )
    return {"comparisons": compared, "note": cleaned["note"]}


def _help_text() -> str:
    return (
        "Wondrue (MVP): inspect candidates and evidence, start real or "
        "synthetic evaluations, explain Raphael decisions, compare "
        "Kratky scenarios, inspect Uriel packages, preview/run/inspect "
        "Priscilla cohort simulations, or start/stop/resume/inspect "
        "bounded discovery. Modes: read-only (inspect only), ask "
        "(approve before acting), full (act within limits)."
    )


def _json_list_arg(args: dict[str, Any], key: str, what: str) -> list[Any] | None:
    """Parse an optional JSON-list intent arg (scalars only upstream)."""
    import json

    raw = args.get(key)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise WondrueError(f"{what} must be a JSON list string")
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise WondrueError(f"{what} is not valid JSON") from exc
    if not isinstance(value, list):
        raise WondrueError(f"{what} must be a JSON list")
    return value


def _burst_cells(args: dict[str, Any]) -> list[Any]:
    from app.discovery_runner import RunnerCell

    raw = _json_list_arg(args, "cells", "cells")
    if not raw:
        raise WondrueError("cells (JSON list) is required")
    cells = []
    for item in raw:
        if not isinstance(item, dict):
            raise WondrueError("each cell must be an object")
        bbox = item.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, (int, float)) for value in bbox)
        ):
            raise WondrueError("each cell needs bbox [south, west, north, east]")
        try:
            cells.append(
                RunnerCell(
                    county=str(item.get("county", "")),
                    category=str(item.get("category", "")),
                    bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                    source=str(item.get("source", "overpass") or "overpass"),
                )
            )
        except (ValueError, TypeError) as exc:
            raise WondrueError(f"invalid cell: {exc}") from exc
    return cells


def _acquire_request(args: dict[str, Any]) -> bool:
    """Parse the burst acquisition request (fail closed on ambiguity)."""
    raw = args.get("acquire", False)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    if isinstance(raw, str) and raw.strip().lower() in (
        "0", "1", "true", "false", "yes", "no",
    ):
        return raw.strip().lower() in ("1", "true", "yes")
    raise WondrueError("acquire must be a boolean")


def _burst_bounds(args: dict[str, Any]) -> tuple[int, float, int, int]:
    """Strict burst bounds shared by preview and execution (fail closed)."""
    try:
        max_passes = int(args.get("max_passes", 1))
        max_duration = float(args.get("max_duration_seconds", 600))
        max_created = int(args.get("max_created", 50))
    except (ValueError, TypeError) as exc:
        raise WondrueError("burst bounds must be numeric") from exc
    raw_evaluations = args.get("max_evaluations", WONDRUE_BURST_MAX_EVALUATIONS)
    if isinstance(raw_evaluations, bool):
        raise WondrueError("burst bounds must be numeric")
    try:
        evaluations = float(raw_evaluations)
    except (ValueError, TypeError) as exc:
        raise WondrueError("burst bounds must be numeric") from exc
    if not evaluations.is_integer():
        raise WondrueError("burst bounds must be numeric")
    max_evaluations = int(evaluations)
    if max_passes < 1 or max_passes > WONDRUE_BURST_MAX_PASSES:
        raise WondrueError(f"max_passes must be 1..{WONDRUE_BURST_MAX_PASSES}")
    if max_duration <= 0 or max_duration > WONDRUE_BURST_MAX_DURATION_SECONDS:
        raise WondrueError(
            f"max_duration_seconds must be within (0, {WONDRUE_BURST_MAX_DURATION_SECONDS}]"
        )
    if max_evaluations < 0 or max_evaluations > WONDRUE_BURST_MAX_EVALUATIONS:
        raise WondrueError(
            f"max_evaluations must be 0..{WONDRUE_BURST_MAX_EVALUATIONS}"
        )
    return max_passes, max_duration, max_created, max_evaluations


def _burst_report(
    args: dict[str, Any], *, resume: bool = False, acquire_approved: bool = False
) -> dict[str, Any]:
    """Execute one request-safe bounded discovery burst (worker-owned conns)."""
    from app import discovery_runner as runner_module

    scope = str(args.get("scope", "") or "")
    if not scope:
        raise WondrueError("scope is required")
    max_passes, max_duration, max_created, max_evaluations = _burst_bounds(args)
    config = runner_module.RunnerConfig(
        scope=scope,
        policy=str(args.get("policy", "pilot") or "pilot"),
        cells=_burst_cells(args),
        max_passes=max_passes,
        max_duration_seconds=max_duration,
        max_created_total=max_created,
        resume=resume,
        created_by="wondrue-command",
        acquire_approved=acquire_approved,
        max_evaluations_per_pass=max_evaluations,
    )

    def _connect() -> Any:
        from app.properties_db import get_connection

        return get_connection()

    try:
        return runner_module.run_worker(_connect, config)
    except runner_module.RunnerError as exc:
        raise WondrueError(str(exc)) from exc


def _execute_real(
    intent: str, args: dict[str, Any], conn: Any, *, acquire_authorized: bool = False
) -> dict[str, Any]:
    from app import mvp as mvp_module
    from app import priscilla as priscilla_module
    from app import uriel as uriel_module
    from app.workflow_db import get_record_in_transaction

    if intent == "start_real_evaluation":
        assignment_id = str(args.get("assignment_id", ""))
        query = str(args.get("query", ""))
        if not assignment_id or not query:
            raise WondrueError("assignment_id and query are required")
        site = args.get("site_inputs") if isinstance(args.get("site_inputs"), dict) else None
        if conn is None:
            # Audited wrapper owns the coordinated transaction.
            summary = mvp_module.start_evaluation_audited(
                assignment_id, query, site_inputs=site, approved=True
            )
            return {"summary": summary}
        from app.workflow import digest_payload
        from app.workflow_db import create_run_in_transaction as _create

        run = _create(
            conn,
            idempotency_key=f"mvp-{digest_payload({'a': assignment_id, 'q': query})[:16]}",
            limits={"brave_attempts": 9},
        )
        summary = mvp_module.evaluate_real(
            conn, run.run_id, assignment_id, query,
            site_inputs=site, approved=True,
        )
        return {"summary": summary}
    if intent in ("inspect_candidate", "inspect_evidence", "explain_decision", "inspect_review") and conn is None:
        raise WondrueError("real target requires a database connection")
    if intent == "inspect_candidate":
        record = get_record_in_transaction(
            conn, UUID(str(args.get("run_id"))), "dossier_version", str(args.get("key", "main"))
        )
        return {"dossier": record.payload if record else None}
    if intent == "inspect_evidence":
        record = get_record_in_transaction(
            conn, UUID(str(args.get("run_id"))), "research_artifact", str(args.get("key", ""))
        )
        return {"artifact": record.payload if record else None}
    if intent == "explain_decision":
        record = get_record_in_transaction(
            conn, UUID(str(args.get("run_id"))), "raphael_decision", str(args.get("key", ""))
        )
        return {"decision": record.payload if record else None}
    if intent == "inspect_review":
        record = get_record_in_transaction(
            conn, UUID(str(args.get("run_id"))), "identity_review",
            str(args.get("key", "dossier-association")),
        )
        return {"review": record.payload if record else None}
    if intent == "stop":
        if conn is None:
            mvp_module.stop_run_audited(UUID(str(args.get("run_id"))))
            return {"stopped": str(args.get("run_id"))}
        mvp_module.stop_run(conn, UUID(str(args.get("run_id"))))
        return {"stopped": str(args.get("run_id"))}
    if intent == "inspect_packages":
        if conn is None:
            raise WondrueError("real target requires a database connection")
        try:
            limit = int(args.get("limit", 50))
        except (ValueError, TypeError) as exc:
            raise WondrueError("limit must be an integer") from exc
        if limit < 1 or limit > 200:
            raise WondrueError("limit must be 1..200")
        return {"packages": uriel_module.list_packages_in_transaction(conn, limit=limit)}
    if intent == "preview_simulation":
        if conn is None:
            raise WondrueError("real target requires a database connection")
        entries = _json_list_arg(args, "package_entries", "package_entries")
        return priscilla_module.preview_simulation_in_transaction(
            conn,
            cohort=str(args.get("cohort", "") or ""),
            package_entries=[str(item) for item in entries] if entries else None,
            seed=str(args.get("seed", "") or ""),
        )
    if intent == "run_simulation":
        if conn is None:
            raise WondrueError("real target requires a database connection")
        entries = _json_list_arg(args, "package_entries", "package_entries")
        cohort = str(args.get("cohort", "") or "")
        seed = str(args.get("seed", "") or "")
        key = str(args.get("idempotency_key", "") or f"wondrue:{cohort or 'all'}:{seed}")
        run = priscilla_module.run_simulation_in_transaction(
            conn,
            idempotency_key=key,
            cohort=cohort,
            package_entries=[str(item) for item in entries] if entries else None,
            actor="wondrue",
            seed=seed,
        )
        return {"simulation": run}
    if intent == "inspect_simulation":
        if conn is None:
            raise WondrueError("real target requires a database connection")
        sim_id = str(args.get("simulation_id", "") or "")
        if not sim_id:
            raise WondrueError("simulation_id is required")
        run = priscilla_module.get_simulation_in_transaction(conn, sim_id)
        if run is None:
            raise WondrueError("unknown simulation run")
        funnel = priscilla_module.funnel_in_transaction(conn, simulation_id=run["sim_id"])
        return {"run": run, "funnel": funnel}
    if intent == "inspect_discovery":
        if conn is None:
            raise WondrueError("real target requires a database connection")
        from app import discovery_runner as runner_module

        scope = str(args.get("scope", "") or "")
        if not scope:
            raise WondrueError("scope is required")
        return {"run": runner_module.latest_run_in_transaction(conn, scope)}
    if intent in ("start_discovery", "resume_discovery"):
        requested = _acquire_request(args)
        if requested and not acquire_authorized:
            raise WondrueAuthError("evidence acquisition requires the operator grant")
        return {
            "burst": _burst_report(
                args, resume=(intent == "resume_discovery"),
                acquire_approved=bool(requested and acquire_authorized),
            )
        }
    if intent == "stop_discovery":
        if conn is None:
            raise WondrueError("real target requires a database connection")
        from app import discovery_runner as runner_module

        scope = str(args.get("scope", "") or "")
        if not scope:
            raise WondrueError("scope is required")
        return {"stop_requested": runner_module.request_stop_in_transaction(conn, scope)}
    if intent == "help":
        return {"help": _help_text()}
    raise WondrueError(f"unknown intent {intent!r}")


def handle_command(
    *,
    mode: str,
    max_mode: str,
    text: str,
    target: str = "demo",
    approval_token: str | None = None,
    use_live: bool = False,
    intent_override: dict[str, Any] | None = None,
    store: DemoStore | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Interpret (or accept a validated intent) and enforce mode gates.

    ``intent_override`` exists for tests and for callers that already
    hold validated structured input; raw model text always passes
    through validation.

    ``max_mode`` is the caller's server-side grant, derived from the
    authenticated credential at the route. The requested mode is capped
    by it: supplying ``"mode": "full"`` confers no authority by itself.
    """
    if mode not in MODES:
        raise WondrueError(f"unknown mode: {mode!r}")
    if max_mode not in MODES:
        raise WondrueAuthError(f"unknown grant: {max_mode!r}")
    if _GRANT_ORDER[mode] > _GRANT_ORDER[max_mode]:
        raise WondrueAuthError(
            f"mode {mode!r} exceeds granted authority ({max_mode})"
        )
    if target not in TARGETS:
        raise WondrueError(f"unknown target: {target!r}")
    model_result: Any = None
    if intent_override is not None:
        intent_args = validate_intent(intent_override)
    else:
        intent_args, model_result = interpret_command(text, use_live=use_live, mode=mode)
    intent = str(intent_args["intent"])
    args = dict(intent_args["args"])
    if intent in CONSEQUENTIAL_INTENTS:
        if mode == "read-only":
            raise WondrueError(f"intent {intent!r} is blocked in read-only mode")
        if mode == "ask" and approval_token is None:
            token = APPROVALS.issue(intent, args, target)
            return {
                "outcome": "preview",
                "intent": intent,
                "target": target,
                "preview": preview_intent(intent, args, target),
                "approval_token": token,
                "model": _model_label(model_result),
            }
        if mode == "ask":
            assert approval_token is not None
            entry = APPROVALS.redeem(approval_token)
            if entry["intent"] != intent or entry["target"] != target:
                raise WondrueError("approval token does not match this command")
            args = entry["args"]
    if intent == "compare_scenarios":
        proposal_adapter = _adapter(use_live, mode)
        proposal = _complete(
            proposal_adapter,
            ModelRequest("propose_configs", {"target": target}),
            approved=True,
        )
        site_inputs = args.get("site_inputs") if isinstance(args.get("site_inputs"), dict) else None
        comparison = compare_with_proposal(proposal.result, site_inputs)
        if target == "demo":
            comparison["label"] = DEMO_LABEL
        return {
            "outcome": "result",
            "intent": intent,
            "target": target,
            "data": comparison,
            "model": _model_label(proposal),
        }
    if target == "demo":
        data = _execute_demo(intent, args, store or DEMO)
    else:
        data = _execute_real(intent, args, conn, acquire_authorized=(max_mode == "full"))
    return {
        "outcome": "result",
        "intent": intent,
        "target": target,
        "data": data,
        "model": _model_label(model_result),
    }


def _model_label(model_result: Any) -> dict[str, Any]:
    if model_result is None:
        return {"mode": "none"}
    return {
        "mode": model_result.mode,
        "provider": model_result.provider,
        "model": model_result.model,
    }
