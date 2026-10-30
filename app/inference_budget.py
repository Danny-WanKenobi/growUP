"""Shared PostgreSQL-backed inference budget (Baseten/Meta capabilities).

Enforcement is a token-unit allowance, not a monetary ceiling: every
live call reserves conservative token units before any provider
request, and reported overruns are charged back to the same
allowance. USD figures are deterministic application estimates over
owner-configured provisional planning rates; they are never provider
billing and never gate execution by themselves.

Request limits come from one centralized throttle profile table
(low/medium/high/extra_high): application resource profiles, not
guaranteed measures of intelligence or accuracy. Administrator
ceilings can only reduce profile limits, never raise them.

The allowance is competition-wide, granted by explicit owner
configuration, and never resets automatically. Reservations are
all-or-nothing conditional updates (atomic under Postgres row
locking), so concurrent callers cannot overspend. Consumption is
tracked in three honest states: estimated at request time, reserved
before the call, actual once the provider reports usage; ambiguous
outcomes stay explicitly unresolved with their reservation retained.
Reconciliation is idempotent per request: a repeated reconcile never
double-charges.

Thresholds on consumed fraction:
- 50%: warn and report usage by agent/capability.
- 75%: defer optional requests (returned, never silently dropped).
- 90%: large batches need an ask-mode approval token; the configured
  demonstration reserve stays protected.
- 100%: refuse further spending until the owner raises the allowance.
"""

from __future__ import annotations

import json as _json
import os
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from app.db import DatabaseUnavailableError

ALLOWANCE_ENV_VAR = "INFERENCE_TOKEN_ALLOWANCE"
LEGACY_RATE_ENV_VAR = "INFERENCE_USD_PER_1K_TOKENS"
INPUT_RATE_ENV_VAR = "INFERENCE_USD_PER_1M_INPUT"
OUTPUT_RATE_ENV_VAR = "INFERENCE_USD_PER_1M_OUTPUT"
PRICING_MULTIPLIER_ENV_VAR = "INFERENCE_PRICING_MULTIPLIER"
PRICING_STATUS_ENV_VAR = "INFERENCE_PRICING_STATUS"
PRICING_SOURCE_ENV_VAR = "INFERENCE_PRICING_SOURCE"
PRICING_DATE_ENV_VAR = "INFERENCE_PRICING_DATE"
DEMO_RESERVE_ENV_VAR = "INFERENCE_DEMO_RESERVE"
LARGE_BATCH_ENV_VAR = "INFERENCE_LARGE_BATCH_TOKENS"
TOKENS_PER_IMAGE_ENV_VAR = "INFERENCE_TOKENS_PER_IMAGE"
MAX_INPUT_CHARS_ENV_VAR = "INFERENCE_MAX_PROMPT_CHARS"

DEFAULT_LARGE_BATCH_TOKENS = 8000
DEFAULT_MAX_INPUT_CHARS = 60000

WARN_FRACTION = 0.50
DEFER_FRACTION = 0.75
PROTECT_FRACTION = 0.90

# Centralized throttle profiles: canonical name -> resource limits.
# Shared by every capability and requesting agent; larger settings
# permit more context and completion space but guarantee nothing
# about result quality.
INFERENCE_PROFILES: dict[str, dict[str, int]] = {
    "low": {"max_completion_tokens": 1024, "max_input_bytes": 8192},
    "medium": {"max_completion_tokens": 2048, "max_input_bytes": 16384},
    "high": {"max_completion_tokens": 4096, "max_input_bytes": 32768},
    "extra_high": {"max_completion_tokens": 8192, "max_input_bytes": 60000},
}
DEFAULT_INFERENCE_PROFILE = "medium"


class BudgetExhausted(Exception):
    """Insufficient inference allowance remains."""


class BudgetConfigError(ValueError):
    """The inference budget configuration is missing or invalid."""


class BudgetDeferred(Exception):
    """An optional request was deferred at the 75% threshold."""

    def __init__(self, message: str, snapshot: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.snapshot = snapshot or {}


def _positive_int(raw: str, name: str, *, allow_zero: bool) -> int:
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError) as exc:
        raise BudgetConfigError(f"invalid {name}") from exc
    if value < 0 or (value == 0 and not allow_zero):
        raise BudgetConfigError(f"invalid {name}")
    return value


def get_allowance() -> int:
    """Configured competition-wide token allowance (0 = live disabled)."""
    return _positive_int(
        os.getenv(ALLOWANCE_ENV_VAR, "0"), "inference token allowance",
        allow_zero=True,
    )


def get_demo_reserve() -> int:
    """Token units protected for the demonstration (default 0)."""
    return _positive_int(
        os.getenv(DEMO_RESERVE_ENV_VAR, "0"), "inference demo reserve",
        allow_zero=True,
    )


def get_large_batch_tokens() -> int:
    """Single-request size needing token approval at/after 90%."""
    return _positive_int(
        os.getenv(LARGE_BATCH_ENV_VAR, str(DEFAULT_LARGE_BATCH_TOKENS)),
        "inference large-batch bound",
        allow_zero=False,
    )


def get_tokens_per_image() -> int:
    """Configured token bound per supplied image.

    No reliable media bound can be derived here, so live media
    execution is refused until the owner explicitly configures one.
    """
    raw = (os.getenv(TOKENS_PER_IMAGE_ENV_VAR, "") or "").strip()
    if not raw:
        raise BudgetConfigError(
            "media token bound is not configured; live media execution refused"
        )
    return _positive_int(raw, "inference image token bound", allow_zero=False)


def _pricing_decimal(raw: str, name: str, *, positive: bool) -> Decimal:
    try:
        value = Decimal(raw.strip())
    except Exception as exc:
        raise BudgetConfigError(f"invalid {name}") from exc
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise BudgetConfigError(f"invalid {name}")
    return value


def get_pricing_basis() -> dict[str, Any]:
    """Owner-configured provisional pricing basis (planning only).

    Separate reference input/output rates per million tokens plus a
    safety multiplier; planning rates are reference x multiplier. A
    legacy blended rate must never silently override this basis, so
    its presence is a loud configuration error, not a fallback.
    """
    if (os.getenv(LEGACY_RATE_ENV_VAR, "") or "").strip():
        raise BudgetConfigError(
            "legacy INFERENCE_USD_PER_1K_TOKENS is set; remove it and "
            "configure the separate-rate pricing basis instead"
        )
    reference_input = _pricing_decimal(
        os.getenv(INPUT_RATE_ENV_VAR, "") or "",
        "inference input reference rate", positive=False,
    )
    reference_output = _pricing_decimal(
        os.getenv(OUTPUT_RATE_ENV_VAR, "") or "",
        "inference output reference rate", positive=False,
    )
    multiplier = _pricing_decimal(
        os.getenv(PRICING_MULTIPLIER_ENV_VAR, "") or "",
        "inference pricing multiplier", positive=True,
    )
    status = (os.getenv(PRICING_STATUS_ENV_VAR, "") or "").strip()
    source = (os.getenv(PRICING_SOURCE_ENV_VAR, "") or "").strip()
    checked = (os.getenv(PRICING_DATE_ENV_VAR, "") or "").strip()
    if not status or not source or not checked:
        raise BudgetConfigError(
            "inference pricing status/source/date are not configured; "
            "live execution refused"
        )
    return {
        "reference_input_per_1m_usd": reference_input,
        "reference_output_per_1m_usd": reference_output,
        "multiplier": multiplier,
        "planning_input_per_1m_usd": reference_input * multiplier,
        "planning_output_per_1m_usd": reference_output * multiplier,
        "status": status,
        "source": source,
        "checked": checked,
    }


def pricing_basis_record(basis: dict[str, Any], estimate_label: str) -> dict[str, str]:
    """JSON-safe pricing provenance retained per ledger row."""
    return {
        "status": str(basis["status"]),
        "source": str(basis["source"]),
        "checked": str(basis["checked"]),
        "reference_input_per_1m_usd": str(basis["reference_input_per_1m_usd"]),
        "reference_output_per_1m_usd": str(basis["reference_output_per_1m_usd"]),
        "multiplier": str(basis["multiplier"]),
        "planning_input_per_1m_usd": str(basis["planning_input_per_1m_usd"]),
        "planning_output_per_1m_usd": str(basis["planning_output_per_1m_usd"]),
        "estimate_label": estimate_label,
    }


def _valid_tokens(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def estimate_cost_from_usage(
    usage: dict[str, Any], basis: dict[str, Any]
) -> tuple[Decimal | None, str]:
    """Deterministic planning estimate from reported usage.

    Separate input/output planning rates when both parts are valid;
    otherwise the reported total is conservatively estimated at the
    higher (output) planning rate and labeled as a fallback. Usage
    ``completion_tokens`` already includes any reasoning tokens per
    provider convention, so reasoning is never added again. Returns
    (estimate, label); unknown usage yields (None, "unknown").
    """
    million = Decimal(1000000)
    prompt = _valid_tokens(usage.get("prompt_tokens"))
    completion = _valid_tokens(usage.get("completion_tokens"))
    if prompt is not None and completion is not None:
        estimate = (
            Decimal(prompt) * basis["planning_input_per_1m_usd"]
            + Decimal(completion) * basis["planning_output_per_1m_usd"]
        ) / million
        return (
            estimate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            "split input/output planning rates",
        )
    total = _valid_tokens(usage.get("total_tokens"))
    if total is not None:
        estimate = (
            Decimal(total) * basis["planning_output_per_1m_usd"] / million
        )
        return (
            estimate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
            "total-only fallback at output planning rate",
        )
    return None, "unknown usage"


def preview_cost_exposure(
    max_input_units: int, max_completion_tokens: int, basis: dict[str, Any]
) -> Decimal:
    """Maximum planning-cost exposure for a profile (pre-execution).

    Input headroom is priced at the input planning rate and completion
    headroom at the output planning rate: a labeled upper bound, not a
    prediction and never provider billing.
    """
    return (
        (
            Decimal(max(0, max_input_units))
            * basis["planning_input_per_1m_usd"]
            + Decimal(max(0, max_completion_tokens))
            * basis["planning_output_per_1m_usd"]
        )
        / Decimal(1000000)
    ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def describe_profile_preview(
    profile: str | None,
    image_count: int,
    budget: dict[str, Any] | None,
) -> dict[str, Any]:
    """Pre-execution consumption disclosure for one operation.

    Selected setting, effective limits, maximum calls for this
    capability (always one), maximum reservation and planning-cost
    exposure, pricing status, and remaining allowance when a budget
    snapshot is available. Never spends, never calls the provider.

    When images are supplied but the per-image bound is missing or
    invalid, media exposure is unknown: the totals are marked
    incomplete rather than presented as a complete estimate that
    silently excludes image consumption. Execution stays fail-closed.
    """
    limits = resolve_effective_limits(profile, None)
    images = max(0, int(image_count or 0))
    media_note: str | None = None
    try:
        media = images * get_tokens_per_image() if images else 0
    except BudgetConfigError as exc:
        media = 0
        if images:
            media_note = (
                f"live media execution unavailable: {exc}; image "
                "consumption is unknown, so totals below are incomplete"
            )
    try:
        basis = get_pricing_basis()
    except BudgetConfigError as exc:
        pricing: dict[str, Any] = {"configured": False, "note": str(exc)}
        exposure: Any = None
    else:
        exposure = str(preview_cost_exposure(
            limits["effective_input_bytes"] + media,
            limits["effective_completion_tokens"],
            basis,
        ))
        if media_note is not None:
            exposure = None
        pricing = {
            "configured": True,
            "status": basis["status"],
            "planning_input_per_1m_usd": str(basis["planning_input_per_1m_usd"]),
            "planning_output_per_1m_usd": str(basis["planning_output_per_1m_usd"]),
            "note": "provisional planning estimate, never provider billing",
        }
    if media_note is not None:
        max_reservation: Any = None
    else:
        max_reservation = (
            limits["effective_input_bytes"]
            + media
            + limits["effective_completion_tokens"]
        )
    block: dict[str, Any] = {
        "profile": limits["profile"],
        "requested_limits": {
            "completion_tokens": limits["requested_completion_tokens"],
            "input_bytes": limits["requested_input_bytes"],
        },
        "effective_limits": {
            "completion_tokens": limits["effective_completion_tokens"],
            "input_bytes": limits["effective_input_bytes"],
        },
        "capped_by_admin": limits["capped_by_admin"],
        "max_provider_calls": 1,
        "max_reservation_units": max_reservation,
        "max_cost_exposure_usd": exposure,
        "media_note": media_note,
        "pricing": pricing,
    }
    if budget is not None:
        allowance = int(budget.get("allowance", 0) or 0)
        used = int(budget.get("used", 0) or 0)
        block["allowance_remaining_units"] = max(0, allowance - used)
        warnings = list(budget.get("warnings", []) or [])
        if allowance <= 0:
            warnings.append("live spending is disabled (allowance is 0)")
        else:
            try:
                fraction = float(budget.get("fraction", 0) or 0)
            except (TypeError, ValueError):
                fraction = 0.0
            if fraction >= PROTECT_FRACTION:
                warnings.append(
                    "90% consumed: large batches need approval and the "
                    "demonstration reserve stays protected"
                )
            elif fraction >= DEFER_FRACTION:
                warnings.append(
                    "75% consumed: optional requests are deferred visibly"
                )
            elif fraction >= WARN_FRACTION:
                warnings.append("50% consumed: review usage by agent/capability")
        block["budget_warnings"] = warnings
    else:
        block["allowance_remaining_units"] = None
        block["budget_warnings"] = ["allowance unknown without a database"]
    return block


def global_input_ceiling_chars() -> int:
    """Administrator-controlled serialized-input cap (characters)."""
    try:
        value = int(os.getenv(
            MAX_INPUT_CHARS_ENV_VAR, str(DEFAULT_MAX_INPUT_CHARS)
        ))
    except ValueError:
        return DEFAULT_MAX_INPUT_CHARS
    return max(1, value)


def resolve_effective_limits(
    profile: str | None, max_tokens: int | None
) -> dict[str, Any]:
    """Resolve one request's profile into requested/effective limits.

    Unknown profiles are rejected. Administrator ceilings can only
    reduce profile limits; requested-vs-effective is reported so a
    reduction is never silent. ``max_tokens`` is an optional extra
    caller cap applied on top of the profile.
    """
    from app.model_adapter import global_completion_ceiling

    name = profile if profile is not None else DEFAULT_INFERENCE_PROFILE
    spec = INFERENCE_PROFILES.get(str(name))
    if spec is None:
        raise BudgetConfigError(
            f"unknown inference profile: {name!r} "
            f"(expected one of {sorted(INFERENCE_PROFILES)})"
        )
    requested_completion = spec["max_completion_tokens"]
    if max_tokens is not None:
        try:
            caller_cap = int(max_tokens)
        except (TypeError, ValueError) as exc:
            raise BudgetConfigError("max_tokens must be an integer") from exc
        requested_completion = min(requested_completion, max(1, caller_cap))
    requested_input = spec["max_input_bytes"]
    effective_completion = min(requested_completion, global_completion_ceiling())
    effective_input = min(requested_input, global_input_ceiling_chars())
    return {
        "profile": str(name),
        "requested_completion_tokens": requested_completion,
        "requested_input_bytes": requested_input,
        "effective_completion_tokens": max(1, effective_completion),
        "effective_input_bytes": max(1, effective_input),
        "capped_by_admin": (
            effective_completion < requested_completion
            or effective_input < requested_input
        ),
    }


def bound_input_units(text_bytes: int, image_count: int) -> int:
    """Conservative input bound in token units (an assumption, not proof).

    Text is charged at one unit per UTF-8 byte of the exact
    transmitted body: byte-pair-style tokenizers typically stay far
    below this, but no upper-bound guarantee is claimed for the
    configured model without tokenizer support. The assumption is
    closed by overrun accounting — reported usage past the
    reservation is charged back, so underestimation cannot hide
    spend. Images use the explicitly configured per-image bound
    (live media without one is refused). Output is bounded
    separately by max_tokens.
    """
    bound = max(0, int(text_bytes))
    images = max(0, int(image_count))
    if images:
        bound += images * get_tokens_per_image()
    return bound


def _allowance_row_in_transaction(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inference_allowance (id, allowance_units, used_units, "
            "demo_reserve_units) VALUES (1, 0, 0, 0) "
            "ON CONFLICT (id) DO NOTHING"
        )
        cur.execute(
            "SELECT allowance_units, used_units, demo_reserve_units "
            "FROM inference_allowance WHERE id = 1"
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover - row was just ensured
        raise DatabaseUnavailableError("inference_unavailable")
    data = dict(row)
    return {
        "allowance": int(data.get("allowance_units", 0) or 0),
        "used": int(data.get("used_units", 0) or 0),
        "demo_reserve": int(data.get("demo_reserve_units", 0) or 0),
    }


def read_budget_in_transaction(conn: Any) -> dict[str, Any]:
    """Read allowance state plus configured thresholds (no reservation)."""
    state = _allowance_row_in_transaction(conn)
    configured = get_allowance()
    allowance = max(state["allowance"], 0)
    # The live configured allowance governs; the row carries durable use.
    effective = configured if configured else allowance
    used = state["used"]
    fraction = (used / effective) if effective else 1.0
    return {
        "allowance": effective,
        "used": used,
        "remaining": max(0, effective - used),
        "fraction": fraction,
        "demo_reserve": max(state["demo_reserve"], get_demo_reserve()),
        "thresholds": {
            "warn": WARN_FRACTION,
            "defer": DEFER_FRACTION,
            "protect": PROTECT_FRACTION,
        },
    }


def usage_by_agent_in_transaction(conn: Any) -> list[dict[str, Any]]:
    """Consumed units grouped by agent/capability (for the 50% warning).

    Consumption is reservations plus reconciled overruns over live,
    unreleased rows; released reservations are excluded from
    consumed totals and reported separately. Replays record zero
    units. Consumed units summed here reconcile with the singleton
    allowance's used total.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT agent, capability, COUNT(*) AS calls, "
            "COALESCE(SUM(CASE WHEN outcome <> 'released' "
            "THEN reserved_units + excess_units ELSE 0 END), 0) AS consumed, "
            "COALESCE(SUM(CASE WHEN outcome = 'released' "
            "THEN reserved_units ELSE 0 END), 0) AS released "
            "FROM inference_usage WHERE live = TRUE "
            "GROUP BY agent, capability ORDER BY agent, capability"
        )
        rows = cur.fetchall() or []
    return [
        {
            "agent": str(row.get("agent", "")),
            "capability": str(row.get("capability", "")),
            "calls": int(row.get("calls", 0) or 0),
            "consumed_units": int(row.get("consumed", 0) or 0),
            "released_units": int(row.get("released", 0) or 0),
        }
        for row in (dict(item) for item in rows)
    ]


def _snapshot(conn: Any) -> dict[str, Any]:
    state = read_budget_in_transaction(conn)
    state["by_agent_capability"] = usage_by_agent_in_transaction(conn)
    return state


def reserve_in_transaction(
    conn: Any,
    units: int,
    *,
    optional: bool = False,
    demo: bool = False,
    approval_via_token: bool = False,
) -> dict[str, Any]:
    """Atomically reserve token units; enforce every threshold.

    Raises BudgetDeferred for optional requests at/after 75%,
    BudgetExhausted when the allowance (or demo reserve, or the
    large-batch approval rule) refuses the request. On success the
    snapshot carries a 50%+ warning with per-agent usage.
    """
    if units < 1:
        raise ValueError("reservation units must be positive")
    allowance = get_allowance()
    if allowance < 1:
        raise BudgetExhausted("live inference is disabled (no allowance configured)")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inference_allowance (id, allowance_units, used_units, "
            "demo_reserve_units) VALUES (1, %(allowance)s, 0, %(reserve)s) "
            "ON CONFLICT (id) DO UPDATE SET allowance_units = %(allowance)s, "
            "demo_reserve_units = %(reserve)s, updated_at = now()",
            {"allowance": allowance, "reserve": get_demo_reserve()},
        )
        cur.execute(
            "SELECT used_units FROM inference_allowance WHERE id = 1"
        )
        row = cur.fetchone()
    used = int(dict(row or {}).get("used_units", 0) or 0)
    fraction = used / allowance
    if fraction >= 1.0:
        raise BudgetExhausted("inference allowance exhausted")
    if optional and fraction >= DEFER_FRACTION:
        raise BudgetDeferred(
            "optional inference deferred: allowance at/after 75%",
            _snapshot(conn),
        )
    if (
        units > get_large_batch_tokens()
        and fraction >= PROTECT_FRACTION
        and not approval_via_token
    ):
        raise BudgetExhausted(
            "large inference batch at/after 90% needs an ask-mode approval token"
        )
    ceiling = allowance if demo else allowance - get_demo_reserve()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE inference_allowance SET used_units = used_units + %(units)s, "
            "updated_at = now() WHERE id = 1 AND used_units + %(units)s <= %(ceiling)s "
            "RETURNING used_units",
            {"units": units, "ceiling": max(ceiling, 0)},
        )
        granted = cur.fetchone()
    if granted is None:
        raise BudgetExhausted("insufficient inference allowance for request")
    snapshot = _snapshot(conn)
    if snapshot["fraction"] >= WARN_FRACTION:
        snapshot["warning"] = (
            f"inference allowance at {snapshot['fraction'] * 100:.1f}% consumed"
        )
    return snapshot


def _uuid_or_none(value: Any) -> UUID | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, UUID) else UUID(str(value))


def insert_request_in_transaction(conn: Any, record: dict[str, Any]) -> None:
    """Insert one ledger row in status 'reserved' (idempotency-keyed)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inference_usage (request_id, idempotency_key, principal, "
            "agent, capability, dossier_id, dossier_version, run_id, "
            "package_entry, provider, model, prompt_version, schema_version, "
            "request_digest, requested_max_tokens, bound_input_units, "
            "reserved_units, outcome, live, replay_of, profile_name, "
            "effective_max_tokens, effective_max_input_bytes, "
            "pricing_basis) VALUES (%(request_id)s, "
            "%(idempotency_key)s, %(principal)s, %(agent)s, %(capability)s, "
            "%(dossier_id)s, %(dossier_version)s, %(run_id)s, %(package_entry)s, "
            "%(provider)s, %(model)s, %(prompt_version)s, %(schema_version)s, "
            "%(digest)s, %(max_tokens)s, %(bound)s, %(reserved)s, "
            "'reserved', %(live)s, %(replay_of)s, %(profile)s, "
            "%(effective_tokens)s, %(effective_input)s, %(pricing)s)",
            {
                "request_id": str(record["request_id"]),
                "idempotency_key": record.get("idempotency_key"),
                "principal": str(record.get("principal", "") or ""),
                "agent": str(record.get("agent", "") or ""),
                "capability": str(record.get("capability", "") or ""),
                "dossier_id": _uuid_or_none(record.get("dossier_id")),
                "dossier_version": record.get("dossier_version"),
                "run_id": _uuid_or_none(record.get("run_id")),
                "package_entry": _uuid_or_none(record.get("package_entry")),
                "provider": str(record.get("provider", "") or ""),
                "model": str(record.get("model", "") or ""),
                "prompt_version": str(record.get("prompt_version", "") or ""),
                "schema_version": str(record.get("schema_version", "") or ""),
                "digest": str(record.get("request_digest", "") or ""),
                "max_tokens": int(record.get("requested_max_tokens", 0) or 0),
                "bound": int(record.get("bound_input_units", 0) or 0),
                "reserved": int(record.get("reserved_units", 0) or 0),
                "live": bool(record.get("live", True)),
                "replay_of": record.get("replay_of"),
                "profile": str(record.get("profile_name", "") or ""),
                "effective_tokens": int(record.get("effective_max_tokens", 0) or 0),
                "effective_input": int(
                    record.get("effective_max_input_bytes", 0) or 0
                ),
                "pricing": (
                    _json.dumps(record["pricing_basis"])
                    if record.get("pricing_basis") is not None else None
                ),
            },
        )


def find_by_idempotency_key_in_transaction(
    conn: Any, idempotency_key: str
) -> dict[str, Any] | None:
    """Load a ledger row by idempotency key (bound replay reuse).

    The full identity comes back so reuse binds principal, scope,
    capability, model, versions, limits, and dossier context — not
    the payload digest alone.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT request_id, principal, agent, capability, dossier_id, "
            "dossier_version, run_id, package_entry, provider, model, "
            "prompt_version, schema_version, request_digest, "
            "requested_max_tokens, outcome, live, reported_input_tokens, "
            "reported_output_tokens, reported_total_tokens, "
            "estimated_cost_usd, provider_request_id, result, "
            "profile_name, effective_max_tokens, "
            "effective_max_input_bytes, pricing_basis "
            "FROM inference_usage WHERE idempotency_key = %(key)s",
            {"key": idempotency_key},
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


OUTCOMES = ("completed", "failed", "unresolved", "rejected", "released")


def reconcile_in_transaction(
    conn: Any,
    request_id: str,
    *,
    outcome: str,
    reported: dict[str, Any] | None = None,
    estimated_cost_usd: Any = None,
    provider_request_id: str = "",
    error: str = "",
    result: dict[str, Any] | None = None,
    pricing_basis: dict[str, Any] | None = None,
) -> bool:
    """Record a call outcome and charge any reported overrun.

    ``outcome`` is completed, failed, unresolved, or rejected.
    Reported usage is preserved even when semantic validation fails.
    When the provider reports more total units than reserved, the
    excess is charged to the allowance in the same transaction, so
    overruns never disappear from budget totals. The update applies
    only to rows still ``reserved``: repeating a reconcile is a
    no-op and can never double-charge. Returns True when applied.
    A supplied ``pricing_basis`` finalizes the estimate provenance
    recorded at reservation time.
    """
    if outcome not in OUTCOMES or outcome == "released":
        raise ValueError(f"unknown inference outcome: {outcome}")
    reported = dict(reported or {})

    def _tokens(name: str) -> int | None:
        value = reported.get(name)
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    total = _tokens("total_tokens")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE inference_usage SET outcome = %(outcome)s, "
            "reported_input_tokens = %(in_tok)s, "
            "reported_output_tokens = %(out_tok)s, "
            "reported_total_tokens = %(total)s, "
            "estimated_cost_usd = %(cost)s, "
            "provider_request_id = %(provider_ref)s, error = %(error)s, "
            "result = %(result)s, "
            "pricing_basis = COALESCE(%(pricing)s, pricing_basis), "
            "updated_at = now() "
            "WHERE request_id = %(request_id)s AND outcome = 'reserved' "
            "RETURNING reserved_units",
            {
                "outcome": outcome,
                "in_tok": _tokens("prompt_tokens"),
                "out_tok": _tokens("completion_tokens"),
                "total": total,
                "cost": str(estimated_cost_usd) if estimated_cost_usd is not None else None,
                "provider_ref": provider_request_id or "",
                "error": error[:2000],
                "result": _json.dumps(result) if result is not None else None,
                "pricing": (
                    _json.dumps(pricing_basis)
                    if pricing_basis is not None else None
                ),
                "request_id": str(request_id),
            },
        )
        applied = cur.fetchone()
        if applied is None:
            return False
        reserved = int(dict(applied).get("reserved_units", 0) or 0)
        excess = max(0, (total or 0) - reserved)
        if excess:
            cur.execute(
                "UPDATE inference_allowance SET used_units = used_units + %(excess)s, "
                "updated_at = now() WHERE id = 1",
                {"excess": excess},
            )
            cur.execute(
                "UPDATE inference_usage SET excess_units = %(excess)s "
                "WHERE request_id = %(request_id)s",
                {"excess": excess, "request_id": str(request_id)},
            )
    return True


def release_reservation_in_transaction(conn: Any, request_id: str) -> bool:
    """Release a reservation when no provider call happened.

    Used only for pre-provider refusals (for example the concurrency
    bound): the row moves ``reserved`` → ``released`` and its units
    return to the allowance. Like reconcile, this applies once.
    Returns True when applied.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE inference_usage SET outcome = 'released', updated_at = now() "
            "WHERE request_id = %(request_id)s AND outcome = 'reserved' "
            "RETURNING reserved_units",
            {"request_id": str(request_id)},
        )
        applied = cur.fetchone()
        if applied is None:
            return False
        reserved = int(dict(applied).get("reserved_units", 0) or 0)
        cur.execute(
            "UPDATE inference_allowance SET "
            "used_units = GREATEST(0, used_units - %(reserved)s), "
            "updated_at = now() WHERE id = 1",
            {"reserved": reserved},
        )
    return True
