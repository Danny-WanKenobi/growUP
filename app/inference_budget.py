"""Shared PostgreSQL-backed inference budget (Baseten/Meta capabilities).

Enforcement is a token-unit allowance, not a monetary ceiling: every
live call reserves conservative token units before any provider
request, and reported overruns are charged back to the same
allowance. USD figures are deterministic application estimates over
an owner-configured blended rate; they are never provider billing
and never gate execution by themselves.

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

import os
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from app.db import DatabaseUnavailableError

ALLOWANCE_ENV_VAR = "INFERENCE_TOKEN_ALLOWANCE"
RATE_ENV_VAR = "INFERENCE_USD_PER_1K_TOKENS"
DEMO_RESERVE_ENV_VAR = "INFERENCE_DEMO_RESERVE"
LARGE_BATCH_ENV_VAR = "INFERENCE_LARGE_BATCH_TOKENS"
TOKENS_PER_IMAGE_ENV_VAR = "INFERENCE_TOKENS_PER_IMAGE"

DEFAULT_LARGE_BATCH_TOKENS = 8000

WARN_FRACTION = 0.50
DEFER_FRACTION = 0.75
PROTECT_FRACTION = 0.90


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


def get_usd_per_1k_tokens() -> Decimal:
    """Configured blended USD rate; raises when pricing is unknown.

    Live execution is refused until the owner configures a rate:
    pricing is never invented here.
    """
    raw = (os.getenv(RATE_ENV_VAR, "") or "").strip()
    if not raw:
        raise BudgetConfigError(
            "inference pricing is not configured; live execution refused"
        )
    try:
        rate = Decimal(raw)
    except Exception as exc:
        raise BudgetConfigError("invalid inference pricing") from exc
    if not rate.is_finite() or rate < 0:
        raise BudgetConfigError("invalid inference pricing")
    return rate


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


def estimate_cost_usd(total_tokens: int) -> Decimal:
    """Deterministic application cost estimate (never provider billing)."""
    return (
        (Decimal(max(0, total_tokens)) / Decimal(1000) * get_usd_per_1k_tokens())
        .quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    )


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
            "reserved_units, outcome, live, replay_of) VALUES (%(request_id)s, "
            "%(idempotency_key)s, %(principal)s, %(agent)s, %(capability)s, "
            "%(dossier_id)s, %(dossier_version)s, %(run_id)s, %(package_entry)s, "
            "%(provider)s, %(model)s, %(prompt_version)s, %(schema_version)s, "
            "%(digest)s, %(max_tokens)s, %(bound)s, %(reserved)s, "
            "'reserved', %(live)s, %(replay_of)s)",
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
            "estimated_cost_usd, provider_request_id, result "
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
) -> bool:
    """Record a call outcome and charge any reported overrun.

    ``outcome`` is completed, failed, unresolved, or rejected.
    Reported usage is preserved even when semantic validation fails.
    When the provider reports more total units than reserved, the
    excess is charged to the allowance in the same transaction, so
    overruns never disappear from budget totals. The update applies
    only to rows still ``reserved``: repeating a reconcile is a
    no-op and can never double-charge. Returns True when applied.
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

    import json as _json

    total = _tokens("total_tokens")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE inference_usage SET outcome = %(outcome)s, "
            "reported_input_tokens = %(in_tok)s, "
            "reported_output_tokens = %(out_tok)s, "
            "reported_total_tokens = %(total)s, "
            "estimated_cost_usd = %(cost)s, "
            "provider_request_id = %(provider_ref)s, error = %(error)s, "
            "result = %(result)s, updated_at = now() "
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
