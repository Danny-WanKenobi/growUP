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
        "inspect_inference_usage",
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
        "assist_explain",
        "analyze_evidence",
        "explain_economics",
        "draft_outreach",
    }
)
# Consequential intents that may spend inference budget: approval
# binds the selected throttle profile for these, and previews
# disclose consumption.
INFERENCE_INTENTS = frozenset(
    {
        "compare_scenarios",
        "assist_explain",
        "analyze_evidence",
        "explain_economics",
        "draft_outreach",
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


def _capability_context(
    *,
    principal: str = "",
    use_live: bool = False,
    mode: str = "read-only",
    approval_via_token: bool = False,
    demo: bool = False,
    profile: str | None = None,
) -> dict[str, Any]:
    return {
        "principal": principal,
        "use_live": use_live,
        "mode": mode,
        "approval_via_token": approval_via_token,
        "demo": demo,
        "profile": profile,
    }


def interpret_command(
    text: str,
    *,
    use_live: bool,
    mode: str,
    principal: str = "",
    profile: str | None = None,
) -> tuple[dict[str, Any], Any]:
    """Interpret a command into a validated intent plus the model result.

    Every interpretation passes through the shared inference boundary:
    live calls spend from the budgeted allowance, replays are labeled
    and free. The request-scoped throttle profile governs the call;
    model output can never change it.
    """
    from app.inference import complete_capability
    from app.model_adapter import INTERPRET_SCHEMA_VERSION

    try:
        completion = complete_capability(
            agent="wondrue",
            capability="interpret",
            purpose="interpret_command",
            payload={"text": text},
            principal=principal,
            profile=profile,
            use_live=use_live,
            mode=mode,
            approved=True,
            schema_version=INTERPRET_SCHEMA_VERSION,
            validator=validate_intent,
        )
    except ModelError as exc:
        raise WondrueError(str(exc)) from exc
    result = completion["model_result"]
    return validate_intent(completion["result"]), result


def _inference_preview(profile: str | None, budget: dict[str, Any] | None) -> dict[str, Any]:
    from app.inference_budget import BudgetConfigError, describe_profile_preview

    try:
        return describe_profile_preview(profile, 0, budget)
    except BudgetConfigError as exc:
        raise WondrueError(str(exc)) from exc


def preview_intent(
    intent: str,
    args: dict[str, Any],
    target: str,
    profile: str | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
            "inference": _inference_preview(profile, budget),
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
    if intent == "assist_explain":
        return {
            "operations": ["retrieve stored run/dossier/decision/scenario records", "compose explanation (validated prose)"],
            "inputs": {"run_id": args.get("run_id", ""), "dossier_key": args.get("dossier_key", args.get("key", ""))},
            "effects": "read-only brief plus attributed assistance artifact; no dossier changes",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": "<= 1"},
            "inference": _inference_preview(profile, budget),
        }
    if intent == "analyze_evidence":
        return {
            "operations": ["retrieve stored research artifacts", "suggest conflicts/gaps/research questions (citation-validated)"],
            "inputs": {"run_id": args.get("run_id", "")},
            "effects": "suggestions only; no verification, authorization, or new research",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": "<= 1"},
            "inference": _inference_preview(profile, budget),
        }
    if intent == "explain_economics":
        return {
            "operations": ["retrieve stored scenario/financial records", "explain labeled Michael figures"],
            "inputs": {"run_id": args.get("run_id", ""), "scenario": args.get("scenario", "")},
            "effects": "explanation only; deterministic figures unchanged",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": "<= 1"},
            "inference": _inference_preview(profile, budget),
        }
    if intent == "draft_outreach":
        return {
            "operations": ["load eligible Uriel package", "draft campaign copy (validated shape)"],
            "inputs": {"package_entry": args.get("package_entry", ""), "draft_kind": args.get("draft_kind", "")},
            "effects": "draft content only; never sends, publishes, or automates outreach",
            "cost_bounds": {"brave_attempts": 0, "meta_calls": "<= 1"},
            "inference": _inference_preview(profile, budget),
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
        "Priscilla cohort simulations, start/stop/resume/inspect "
        "bounded discovery, explain stored results and economics, "
        "analyze evidence, draft Uriel copy, or inspect inference "
        "usage. Modes: read-only (inspect only), ask "
        "(approve before acting), full (act within limits). "
        "Each assistance capability spends at most one provider "
        "call; a natural-language request may spend one more for "
        "command interpretation."
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
    intent: str,
    args: dict[str, Any],
    conn: Any,
    *,
    acquire_authorized: bool = False,
    ctx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app import mvp as mvp_module
    from app import priscilla as priscilla_module
    from app import uriel as uriel_module
    from app.workflow_db import get_record_in_transaction

    capability_ctx = dict(ctx or _capability_context())

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
    if intent == "assist_explain":
        from app import assist as assist_module

        return assist_module.assist_explain(conn, args, capability_ctx)
    if intent == "analyze_evidence":
        from app import assist as assist_module

        return assist_module.analyze_evidence(conn, args, capability_ctx)
    if intent == "explain_economics":
        from app import assist as assist_module

        return assist_module.explain_economics(conn, args, capability_ctx)
    if intent == "draft_outreach":
        from app import assist as assist_module

        return assist_module.draft_outreach(conn, args, capability_ctx)
    if intent == "inspect_inference_usage":
        if conn is None:
            raise WondrueError("real target requires a database connection")
        from app import assist as assist_module

        return {"inference_usage": assist_module.inference_usage_snapshot()}
    raise WondrueError(f"unknown intent {intent!r}")


def inference_approval_spec(
    profile: str | None, use_live: bool, principal: str
) -> dict[str, Any]:
    """Canonical inference spec bound into ask-mode approval tokens.

    Small shared representation (mirroring extraction's canonical
    spec): live/replay selection, canonical profile, effective
    material limits, and the authenticated principal. Intent, target,
    and approved arguments ride in the token entry itself.
    """
    from app.inference_budget import resolve_effective_limits

    limits = resolve_effective_limits(profile, None)
    return {
        "profile": limits["profile"],
        "effective_completion_tokens": limits["effective_completion_tokens"],
        "effective_input_bytes": limits["effective_input_bytes"],
        "use_live": bool(use_live),
        "principal": str(principal or ""),
    }


def check_inference_approval_spec(
    approved: Any, current: dict[str, Any]
) -> None:
    """Reject material drift from the approved inference spec.

    Any change — profile, effective limits (including silent
    administrator-ceiling drift), live selection, or principal —
    needs a fresh preview before provider execution.
    """
    if not isinstance(approved, dict):
        raise WondrueError(
            "approval token carries no inference specification; "
            "preview the command again"
        )
    for key in (
        "profile",
        "effective_completion_tokens",
        "effective_input_bytes",
        "use_live",
        "principal",
    ):
        if approved.get(key) != current.get(key):
            raise WondrueError(
                f"approval token does not cover this {key}; "
                "preview the command again"
            )


def preview_budget_snapshot(conn: Any) -> dict[str, Any] | None:
    """Best-effort allowance snapshot for previews (None when unreadable)."""
    if conn is None:
        return None
    try:
        from app.inference_budget import read_budget_in_transaction

        return read_budget_in_transaction(conn)
    except Exception:
        return None


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
    principal: str = "",
    profile: str | None = None,
) -> dict[str, Any]:
    """Interpret (or accept a validated intent) and enforce mode gates.

    ``intent_override`` exists for tests and for callers that already
    hold validated structured input; raw model text always passes
    through validation.

    ``max_mode`` is the caller's server-side grant, derived from the
    authenticated credential at the route. The requested mode is capped
    by it: supplying ``"mode": "full"`` confers no authority by itself.

    ``profile`` is the request-scoped throttle profile (default
    medium), validated server-side before anything else runs. It
    never grants permission or enables spending.
    """
    from app.inference_budget import (
        DEFAULT_INFERENCE_PROFILE,
        BudgetConfigError,
        resolve_effective_limits,
    )

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
    try:
        resolve_effective_limits(profile, None)
    except BudgetConfigError as exc:
        raise WondrueError(str(exc)) from exc
    effective_profile = (
        profile if profile is not None else DEFAULT_INFERENCE_PROFILE
    )
    model_result: Any = None
    if intent_override is not None:
        intent_args = validate_intent(intent_override)
    else:
        intent_args, model_result = interpret_command(
            text, use_live=use_live, mode=mode, principal=principal,
            profile=effective_profile,
        )
    intent = str(intent_args["intent"])
    args = dict(intent_args["args"])
    approval_via_token = False
    if intent in CONSEQUENTIAL_INTENTS:
        if mode == "read-only":
            raise WondrueError(f"intent {intent!r} is blocked in read-only mode")
        if mode == "ask" and approval_token is None:
            issued_args = dict(args)
            if intent in INFERENCE_INTENTS:
                try:
                    issued_args["inference_spec"] = inference_approval_spec(
                        effective_profile, use_live, principal
                    )
                except BudgetConfigError as exc:
                    raise WondrueError(str(exc)) from exc
            token = APPROVALS.issue(intent, issued_args, target)
            interpreted = intent_override is None
            execution_calls = 1 if intent in INFERENCE_INTENTS else 0
            return {
                "outcome": "preview",
                "intent": intent,
                "target": target,
                "preview": preview_intent(
                    intent, args, target, effective_profile,
                    preview_budget_snapshot(conn),
                ),
                "approval_token": token,
                "model": _model_label(model_result),
                "calls": {
                    "interpretation_performed": interpreted,
                    "interpretation_mode": (
                        _model_label(model_result)["mode"]
                        if interpreted else "none"
                    ),
                    "execution_max_calls": execution_calls,
                    "whole_request_max_calls": (
                        (1 if interpreted else 0) + execution_calls
                    ),
                    "note": (
                        "capability execution is at most one provider "
                        "call; a natural-language request may add one "
                        "earlier interpretation call"
                    ),
                },
            }
        if mode == "ask":
            assert approval_token is not None
            entry = APPROVALS.redeem(approval_token)
            if entry["intent"] != intent or entry["target"] != target:
                raise WondrueError("approval token does not match this command")
            args = dict(entry["args"])
            if intent in INFERENCE_INTENTS:
                try:
                    current_spec = inference_approval_spec(
                        effective_profile, use_live, principal
                    )
                except BudgetConfigError as exc:
                    raise WondrueError(str(exc)) from exc
                check_inference_approval_spec(
                    entry["args"].get("inference_spec"), current_spec
                )
                # Execute under the approved specification (identical
                # after the check above); drift needs a fresh preview.
                effective_profile = str(current_spec["profile"])
                args.pop("inference_spec", None)
            approval_via_token = True
    ctx = _capability_context(
        principal=principal,
        use_live=use_live,
        mode=mode,
        approval_via_token=approval_via_token,
        demo=(target == "demo"),
        profile=effective_profile,
    )
    if intent == "compare_scenarios":
        from app.inference import complete_capability
        from app.model_adapter import PROPOSE_SCHEMA_VERSION

        try:
            proposal_completion = complete_capability(
                agent="onesimus",
                capability="propose",
                purpose="propose_configs",
                payload={"target": target},
                principal=principal,
                profile=effective_profile,
                use_live=use_live,
                mode=mode,
                approved=True,
                demo=(target == "demo"),
                approval_via_token=approval_via_token,
                schema_version=PROPOSE_SCHEMA_VERSION,
            )
        except ModelError as exc:
            raise WondrueError(str(exc)) from exc
        proposal = proposal_completion["model_result"]
        site_inputs = args.get("site_inputs") if isinstance(args.get("site_inputs"), dict) else None
        comparison = compare_with_proposal(proposal_completion["result"], site_inputs)
        if target == "demo":
            comparison["label"] = DEMO_LABEL
        return {
            "outcome": "result",
            "intent": intent,
            "target": target,
            "data": comparison,
            "model": _model_label(proposal),
            "inference": {
                "profile": proposal_completion.get("profile"),
                "reported_usage": proposal_completion.get("reported_usage"),
                "billing_unresolved": proposal_completion.get("billing_unresolved"),
                "estimated_cost_usd": proposal_completion.get("estimated_cost_usd"),
                "estimate_label": proposal_completion.get("estimate_label"),
                "pricing": proposal_completion.get("pricing"),
                "live": bool(proposal_completion.get("live", False)),
                "outcome": proposal_completion.get("outcome"),
            },
        }
    if target == "demo":
        data = _execute_demo(intent, args, store or DEMO)
    else:
        data = _execute_real(
            intent, args, conn,
            acquire_authorized=(max_mode == "full"), ctx=ctx,
        )
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
