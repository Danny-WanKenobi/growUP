"""One shared inference boundary for every Baseten/Meta capability.

Every live capability call passes through ``complete_capability``: no
bypass through direct adapter calls. The boundary enforces, in order:
read-only refusal, explicit approval, spending approval, configuration
(pricing, allowance, endpoint), idempotent replay, atomic budget
reservation, provider execution outside long-held transactions,
usage reconciliation, semantic validation, and durable attribution.

Reservations are never refunded once the provider is reached:
failed, rejected, and ambiguous calls keep their units, overruns are
charged back, and ambiguous outcomes stay explicitly unresolved.
Only a pre-provider refusal (concurrency bound) releases its
reservation. Replays are labeled and never spend.

Consumption vocabulary: bound (conservative pre-call upper bound),
reserved (allowance held), actual (provider-reported, charged even
past the reservation), unresolved (ambiguous spend, reservation
retained). Billing is unresolved exactly when a live, unreleased
request has no reported total; capability acceptance (the outcome)
is always distinct from billing resolution. The in-process
concurrency bound is process-local: it protects one worker, not a
multi-worker deployment.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable
from uuid import uuid4

from app.inference_budget import (
    BudgetConfigError,
    BudgetDeferred,
    BudgetExhausted,
    DEFAULT_INFERENCE_PROFILE,
    bound_input_units,
    estimate_cost_from_usage,
    get_allowance,
    get_pricing_basis,
    global_input_ceiling_chars,
    preview_cost_exposure,
    pricing_basis_record,
    read_budget_in_transaction,
    reconcile_in_transaction,
    release_reservation_in_transaction,
    reserve_in_transaction,
    resolve_effective_limits,
    usage_by_agent_in_transaction,
)
from app.model_adapter import (
    MetaLiveAdapter,
    ModelError,
    ModelFailure,
    ModelNotSent,
    ModelRequest,
    ModelTimeout,
    ModelUnresolved,
    ReplayAdapter,
    build_chat_body,
    spending_approved,
)

RETRY_WARNING = (
    "review the ledger before retrying: a new key may double-charge "
    "for processing that already happened"
)
from app.db import DatabaseUnavailableError
from app.properties_db import get_connection

PROMPT_VERSION = "assist-prompts-1"
MAX_CONCURRENT_ENV_VAR = "INFERENCE_MAX_CONCURRENT"
DEFAULT_MAX_CONCURRENT = 4

REPLAYS: dict[str, dict[str, Any]] = {
    "interpret_command": {"intent": "help", "args": {}},
    "propose_configs": {
        "options": [{"config": "bench"}, {"config": "rack3"}],
        "note": "replay: default two-configuration comparison",
    },
    "explain": {"text": "replay: explanation unavailable offline"},
    "assist_explain": {
        "summary": "replay: stored results summarized without live inference",
        "points": [],
        "unknowns": ["replay: live explanation unavailable offline"],
    },
    "analyze_evidence": {
        "conflicts": [],
        "gaps": [],
        "research_questions": [],
    },
    "explain_economics": {
        "summary": "replay: economics explanation unavailable offline",
        "points": [],
    },
    "extract_document": {"claims": []},
    "draft_outreach": {
        "kind": "intro_letter",
        "sections": [
            {
                "heading": "replay draft",
                "body": "replay: draft copy unavailable offline",
            }
        ],
        "uncertainties": ["replay: live drafting unavailable offline"],
    },
}

_semaphore: threading.BoundedSemaphore | None = None
_semaphore_size = 0


def _concurrency_slot() -> threading.BoundedSemaphore:
    global _semaphore, _semaphore_size
    try:
        size = max(1, int(os.getenv(MAX_CONCURRENT_ENV_VAR, str(DEFAULT_MAX_CONCURRENT))))
    except ValueError:
        size = DEFAULT_MAX_CONCURRENT
    if _semaphore is None or _semaphore_size != size:
        _semaphore = threading.BoundedSemaphore(size)
        _semaphore_size = size
    return _semaphore


def read_usage_snapshot() -> dict[str, Any]:
    """Current allowance state plus usage by agent/capability."""
    conn = get_connection()
    try:
        state = read_budget_in_transaction(conn)
        state["by_agent_capability"] = usage_by_agent_in_transaction(conn)
        return state
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _close(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _emit_event(
    *,
    agent: str,
    capability: str,
    request_id: str,
    run_id: Any,
    live: bool,
    outcome: str,
    units: int,
) -> None:
    from app.observability.emitter import emit_nonfatal
    from app.observability.events import (
        AgentEvent,
        AgentId,
        RetentionClass,
        Severity,
    )

    try:
        agent_id = AgentId(agent)
    except ValueError:
        agent_id = AgentId.WONDRUE
    event = AgentEvent.model_validate(
        {
            "agent": agent_id.value,
            "run_id": str(run_id) if run_id else request_id,
            "correlation_id": request_id,
            "event_type": "wondrue.inference.completed",
            "severity": Severity.INFO.value,
            "summary": (
                f"inference {capability} {outcome} "
                f"({'live' if live else 'replay'}, {units} units)"
            ),
            "details": {
                "request_id": request_id,
                "requesting_agent": agent,
                "capability": capability,
                "live": live,
                "outcome": outcome,
                "units": units,
            },
            "evidence_refs": [],
            "retention_class": RetentionClass.ACTIVITY.value,
        }
    )
    emit_nonfatal(event)


def complete_capability(
    *,
    agent: str,
    capability: str,
    purpose: str,
    payload: dict[str, Any],
    principal: str = "",
    dossier_id: Any = None,
    dossier_version: int | None = None,
    run_id: Any = None,
    package_entry: Any = None,
    profile: str | None = None,
    max_tokens: int | None = None,
    use_live: bool = False,
    mode: str = "read-only",
    approved: bool = False,
    optional: bool = False,
    demo: bool = False,
    approval_via_token: bool = False,
    idempotency_key: str | None = None,
    images: list[dict[str, str]] | None = None,
    prompt_version: str = PROMPT_VERSION,
    schema_version: str = "",
    validator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one capability completion through the shared budget boundary.

    ``profile`` selects the centralized throttle profile (default
    medium); ``max_tokens`` is an optional extra caller cap applied
    on top of the profile. Profiles never grant permission or
    enable spending: all grant, approval, spending, concurrency,
    reserve, and allowance checks still apply.
    """
    from app.inference_budget import (
        find_by_idempotency_key_in_transaction,
        insert_request_in_transaction,
    )

    try:
        limits = resolve_effective_limits(profile, max_tokens)
    except BudgetConfigError as exc:
        raise ModelError(str(exc)) from exc
    effective_tokens = limits["effective_completion_tokens"]
    effective_input = limits["effective_input_bytes"]
    request = ModelRequest(purpose, dict(payload), effective_tokens, images)
    if use_live and mode == "read-only":
        raise ModelError("read-only mode uses replay explanations, never live calls")
    if not use_live:
        result = ReplayAdapter(REPLAYS).complete(request)
        cleaned = validator(dict(result.result)) if validator else dict(result.result)
        return {
            "outcome": "replayed",
            "live": False,
            "result": cleaned,
            "model_result": result,
            "request_id": f"replay-{uuid4().hex[:12]}",
            "budget": None,
            "profile": limits,
        }
    if not approved:
        raise ModelError("live inference requires explicit approval")
    if not spending_approved():
        raise ModelError("META_SPEND_APPROVED=1 is not set; live call refused")
    try:
        pricing = get_pricing_basis()
        get_allowance()
    except BudgetConfigError as exc:
        raise ModelError(str(exc)) from exc
    adapter = MetaLiveAdapter()
    _, model_name = adapter.endpoint_config()  # recorded before spend
    # Sized from the exact transmitted body (same builder transport
    # uses), so budget and transport cannot diverge.
    _, text_bytes = build_chat_body(
        purpose, request.payload, request.images, model_name, effective_tokens
    )
    if text_bytes > effective_input:
        raise ModelError(
            f"serialized context is {text_bytes} bytes but the "
            f"{limits['profile']} profile allows {effective_input}; narrow "
            "the request or choose a larger setting (no automatic "
            "upgrade, no retry, no additional call was made)"
        )
    image_count = len(request.images)
    try:
        bound = bound_input_units(text_bytes, image_count)
    except BudgetConfigError as exc:
        raise ModelError(str(exc)) from exc
    units = bound + max(1, effective_tokens)

    conn = get_connection()
    try:
        if idempotency_key:
            found = find_by_idempotency_key_in_transaction(conn, idempotency_key)
            if found is not None and found.get("outcome") == "completed":
                _check_replay_identity(
                    found,
                    request_digest=request.digest(),
                    principal=principal,
                    agent=agent,
                    capability=capability,
                    model=model_name,
                    prompt_version=prompt_version,
                    schema_version=schema_version,
                    max_tokens=effective_tokens,
                    profile=limits["profile"],
                    effective_input_bytes=effective_input,
                    dossier_id=dossier_id,
                    dossier_version=dossier_version,
                    run_id=run_id,
                    package_entry=package_entry,
                )
                stored = found.get("result") or {}
                if isinstance(stored, str):
                    try:
                        stored = json.loads(stored)
                    except ValueError:
                        stored = {}
                stored = stored if isinstance(stored, dict) else {}
                replay_id = f"replay-{uuid4().hex[:12]}"
                try:
                    insert_request_in_transaction(
                        conn,
                        {
                            "request_id": replay_id,
                            "idempotency_key": None,
                            "principal": principal,
                            "agent": agent,
                            "capability": capability,
                            "dossier_id": dossier_id,
                            "dossier_version": dossier_version,
                            "run_id": run_id,
                            "package_entry": package_entry,
                            "provider": MetaLiveAdapter.provider,
                            "model": str(found.get("model", "")),
                            "prompt_version": prompt_version,
                            "schema_version": schema_version,
                            "request_digest": request.digest(),
                            "requested_max_tokens": effective_tokens,
                            "bound_input_units": 0,
                            "reserved_units": 0,
                            "live": False,
                            "replay_of": str(found.get("request_id", "")),
                            "profile_name": limits["profile"],
                            "effective_max_tokens": effective_tokens,
                            "effective_max_input_bytes": effective_input,
                            "pricing_basis": pricing_basis_record(
                                pricing, "replay: original spend is historical"
                            ),
                        },
                    )
                    # Zero-spend replay: no new usage is recorded here;
                    # the original usage stays on the live row, linked
                    # through replay_of as explicitly historical.
                    reconcile_in_transaction(
                        conn,
                        replay_id,
                        outcome="completed",
                        provider_request_id=str(found.get("provider_request_id") or ""),
                        result=stored,
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                replayed = replay_from_ledger(found, stored)
                cleaned = validator(dict(replayed.result)) if validator else dict(replayed.result)
                _emit_event(
                    agent=agent, capability=capability, request_id=replay_id,
                    run_id=run_id, live=False, outcome="replayed", units=0,
                )
                return {
                    "outcome": "replayed",
                    "live": False,
                    "result": cleaned,
                    "model_result": replayed,
                    "request_id": replay_id,
                    "replay_of": str(found.get("request_id", "")),
                    "budget": None,
                    "profile": limits,
                }
            if found is not None:
                if found.get("outcome") == "reserved":
                    raise ModelError("idempotent inference request already in flight")
                raise ModelError(
                    "idempotent request already used by a "
                    f"{found.get('outcome', 'unknown')} attempt; "
                    "retry with a new key"
                )
        request_id = uuid4().hex
        try:
            snapshot = reserve_in_transaction(
                conn,
                units,
                optional=optional,
                demo=demo,
                approval_via_token=approval_via_token,
            )
        except BudgetDeferred as exc:
            conn.rollback()
            return {
                "outcome": "deferred",
                "live": False,
                "result": {},
                "model_result": None,
                "request_id": f"deferred-{uuid4().hex[:12]}",
                "budget": exc.snapshot,
                "deferred_reason": str(exc),
                "profile": limits,
            }
        except BudgetExhausted as exc:
            conn.rollback()
            raise ModelError(str(exc)) from exc
        try:
            insert_request_in_transaction(
                conn,
                {
                    "request_id": request_id,
                    "idempotency_key": idempotency_key,
                    "principal": principal,
                    "agent": agent,
                    "capability": capability,
                    "dossier_id": dossier_id,
                    "dossier_version": dossier_version,
                    "run_id": run_id,
                    "package_entry": package_entry,
                    "provider": MetaLiveAdapter.provider,
                    "model": model_name,
                    "prompt_version": prompt_version,
                    "schema_version": schema_version,
                    "request_digest": request.digest(),
                    "requested_max_tokens": effective_tokens,
                    "bound_input_units": bound,
                    "reserved_units": units,
                    "live": True,
                    "profile_name": limits["profile"],
                    "effective_max_tokens": effective_tokens,
                    "effective_max_input_bytes": effective_input,
                    "pricing_basis": pricing_basis_record(
                        pricing, "pending reported usage"
                    ),
                },
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            message = str(exc).lower()
            if "duplicate" in message or "unique" in message or "already" in message:
                raise ModelError(
                    "idempotent inference request already in flight"
                ) from exc
            raise
    finally:
        _close(conn)

    slot = _concurrency_slot()
    if not slot.acquire(blocking=False):
        # No provider call happened: release the reservation instead
        # of charging it.
        _release_owned(request_id)
        raise ModelError("inference concurrency bound reached; retry later")
    try:
        model_result = adapter.complete(request, approved=True)
    except ModelNotSent as exc:
        # Confirmed pre-provider refusal: nothing could have been
        # billed, so the reservation is released at zero spend.
        _release_owned(request_id)
        raise ModelError(f"{exc}; no spend recorded") from exc
    except (ModelTimeout, ModelUnresolved) as exc:
        usage = dict(getattr(exc, "usage", None) or {})
        error_cost, error_label = _cost_for_usage(usage, pricing)
        _reconcile_owned(
            request_id,
            outcome="unresolved",
            reported=usage,
            estimated_cost_usd=error_cost,
            pricing_basis=pricing_basis_record(pricing, error_label),
            provider_request_id=str(
                getattr(exc, "provider_request_id", "") or ""
            ),
            error=str(exc),
        )
        raise ModelError(
            f"inference billing unresolved ({exc}); "
            f"reservation retained; {RETRY_WARNING}"
        ) from exc
    except ModelFailure as exc:
        failure_usage = dict(getattr(exc, "usage", None) or {})
        failure_cost, failure_label = _cost_for_usage(failure_usage, pricing)
        _reconcile_owned(
            request_id,
            outcome="failed",
            reported=failure_usage,
            estimated_cost_usd=failure_cost,
            pricing_basis=pricing_basis_record(pricing, failure_label),
            provider_request_id=exc.provider_request_id,
            error=str(exc),
        )
        raise
    except ModelError as exc:
        _reconcile_owned(request_id, outcome="failed", error=str(exc))
        raise
    finally:
        slot.release()

    # Provider completion and accepted output are separate stages: the
    # result is validated before it can be marked completed, and usage
    # is preserved on every path.
    usage = dict(model_result.usage or {})
    provider_ref = str(usage.pop("provider_request_id", "") or "")
    total = usage.get("total_tokens")
    cost, cost_label = _cost_for_usage(usage, pricing)
    if validator is not None:
        try:
            cleaned = validator(dict(model_result.result))
        except ModelError as exc:
            _reconcile_owned(
                request_id,
                outcome="rejected",
                reported=usage,
                estimated_cost_usd=cost,
                pricing_basis=pricing_basis_record(pricing, cost_label),
                provider_request_id=provider_ref,
                error=str(exc),
            )
            raise
    else:
        cleaned = dict(model_result.result)
    if total is None:
        outcome = "unresolved"
    else:
        outcome = "completed"
    _reconcile_owned(
        request_id,
        outcome=outcome,
        reported=usage,
        estimated_cost_usd=cost,
        pricing_basis=pricing_basis_record(pricing, cost_label),
        provider_request_id=provider_ref,
        result=model_result.result,
    )
    _emit_event(
        agent=agent, capability=capability, request_id=request_id,
        run_id=run_id, live=True, outcome=outcome, units=units,
    )
    return {
        "outcome": outcome,
        "live": True,
        "result": cleaned,
        "model_result": model_result,
        "request_id": request_id,
        "reported_usage": usage,
        "billing_unresolved": total is None,
        "consumption": {
            "reserved": units,
            "reported_total": total,
            "excess": max(0, int(total) - units) if total is not None else None,
        },
        "estimated_cost_usd": str(cost) if cost is not None else None,
        "estimate_label": cost_label,
        "pricing": pricing_basis_record(pricing, cost_label),
        "profile": limits,
        "provider_request_id": provider_ref,
        "budget": snapshot,
    }


def _check_replay_identity(found: dict[str, Any], **current: Any) -> None:
    """Bind idempotent reuse to the full original identity.

    Principal, scope, capability, model, prompt/schema versions,
    profile plus effective material limits, dossier context, and the
    payload digest must all match; anything else is a different
    operation needing its own key.
    """
    expected = {
        "request_digest": str(found.get("request_digest", "") or ""),
        "principal": str(found.get("principal", "") or ""),
        "agent": str(found.get("agent", "") or ""),
        "capability": str(found.get("capability", "") or ""),
        "model": str(found.get("model", "") or ""),
        "prompt_version": str(found.get("prompt_version", "") or ""),
        "schema_version": str(found.get("schema_version", "") or ""),
        "max_tokens": int(found.get("requested_max_tokens", 0) or 0),
        "profile": str(found.get("profile_name", "") or ""),
        "effective_input_bytes": int(
            found.get("effective_max_input_bytes", 0) or 0
        ),
        "dossier_id": str(found.get("dossier_id") or ""),
        "dossier_version": found.get("dossier_version"),
        "run_id": str(found.get("run_id") or ""),
        "package_entry": str(found.get("package_entry") or ""),
    }
    actual = {
        "request_digest": str(current.get("request_digest", "") or ""),
        "principal": str(current.get("principal", "") or ""),
        "agent": str(current.get("agent", "") or ""),
        "capability": str(current.get("capability", "") or ""),
        "model": str(current.get("model", "") or ""),
        "prompt_version": str(current.get("prompt_version", "") or ""),
        "schema_version": str(current.get("schema_version", "") or ""),
        "max_tokens": int(current.get("max_tokens", 0) or 0),
        "profile": str(current.get("profile", "") or ""),
        "effective_input_bytes": int(
            current.get("effective_input_bytes", 0) or 0
        ),
        "dossier_id": str(current.get("dossier_id") or ""),
        "dossier_version": current.get("dossier_version"),
        "run_id": str(current.get("run_id") or ""),
        "package_entry": str(current.get("package_entry") or ""),
    }
    if expected != actual:
        raise ModelError(
            "idempotency key already used for a different request"
        )


def replay_from_ledger(found: dict[str, Any], stored: dict[str, Any]) -> Any:
    """Build a labeled replay result from a completed live ledger row."""
    from app.model_adapter import ModelResult

    usage = {
        key: found.get(column)
        for key, column in (
            ("prompt_tokens", "reported_input_tokens"),
            ("completion_tokens", "reported_output_tokens"),
            ("total_tokens", "reported_total_tokens"),
        )
        if found.get(column) is not None
    }
    if usage:
        usage["historical"] = True
    return ModelResult(
        provider=str(found.get("provider", "")),
        model=str(found.get("model", "")),
        mode="replay",
        purpose="replay",
        result=dict(stored),
        usage=usage,
        request_digest=str(found.get("request_digest", "")),
    )


def _cost_for_usage(
    usage: dict[str, Any], pricing: dict[str, Any]
) -> tuple[Any, str]:
    """Planning estimate plus label; unknown usage stays unknown."""
    try:
        return estimate_cost_from_usage(usage, pricing)
    except (BudgetConfigError, ValueError, TypeError, ArithmeticError):
        return None, "unknown usage"


def _reconcile_owned(request_id: str, **fields: Any) -> None:
    conn = get_connection()
    try:
        try:
            reconcile_in_transaction(conn, request_id, **fields)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise DatabaseUnavailableError("inference_unavailable") from exc
    finally:
        _close(conn)


def _release_owned(request_id: str) -> None:
    conn = get_connection()
    try:
        try:
            release_reservation_in_transaction(conn, request_id)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            raise DatabaseUnavailableError("inference_unavailable") from exc
    finally:
        _close(conn)
