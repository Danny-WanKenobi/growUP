"""Bounded continuous discovery: repeated small passes, durable checkpoints.

The runner drives existing bounded ``run_discovery`` passes (OSM
adapter, own persistence and dedup) and stops on quota, duration,
budget, failure, Sabbath, or operator request. It never invents
county/city coverage: every cell carries an operator-supplied bbox.

Policies within one runner:

- ``pilot``: serve unmet 36-cell qualification requirements first;
  stop when the canonical quota is satisfied or any limit hits.
- ``timed``: keep discovering/reassessing within an approved
  duration and budget even after the quota is satisfied.

One active worker per scope is enforced by a partial unique index;
checkpoints make stop/resume lossless. Nothing here launches
background work: each invocation is a synchronous bounded burst, so
Wondrue start/resume stays request-safe and long campaigns proceed
as repeated operator-approved bursts (or CLI runs).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from uuid import UUID, uuid4

from app.db import DatabaseNotConfiguredError, DatabaseUnavailableError

try:
    import psycopg

    _DRIVER_ERRORS: tuple[type[BaseException], ...] = (psycopg.Error,)
except ImportError:  # pragma: no cover - dependency present via requirements
    _DRIVER_ERRORS = ()

RUNNER_VERSION = "discovery-runner-1"
POLICIES = ("pilot", "timed")
STATUSES = ("running", "paused", "completed", "aborted")
DEFAULT_PACING_SECONDS = 5.0
DEFAULT_BACKOFF_CAP_SECONDS = 120.0
DEFAULT_CLAIM_LEASE_SECONDS = 1800.0


class RunnerError(Exception):
    """Runner misuse or an unrecoverable worker precondition."""


@dataclass(frozen=True)
class RunnerCell:
    """One county/category work item with an operator-supplied bbox."""

    county: str
    category: str
    bbox: tuple[float, float, float, float]  # south, west, north, east
    source: str = "overpass"

    @property
    def key(self) -> str:
        return f"{self.county}||{self.category}"


@dataclass
class RunnerConfig:
    """Bounds for one worker invocation (all required, no silent open end)."""

    scope: str
    policy: str
    cells: list[RunnerCell] = field(default_factory=list)
    max_passes: int = 1
    max_duration_seconds: float = 600.0
    max_created_total: int = 50
    pass_limit: int = 10
    pass_timeout_seconds: float = 15.0
    pacing_seconds: float = DEFAULT_PACING_SECONDS
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    max_consecutive_failures: int = 3
    claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS
    resume: bool = False
    created_by: str = ""
    acquire_approved: bool = False
    max_evaluations_per_pass: int = 5

    def validate(self) -> None:
        if not str(self.scope or "").strip():
            raise RunnerError("scope is required")
        if self.policy not in POLICIES:
            raise RunnerError(f"unknown policy: {self.policy!r}")
        if not self.cells:
            raise RunnerError("at least one cell is required")
        if self.max_passes < 1:
            raise RunnerError("max_passes must be >= 1")
        if self.max_duration_seconds <= 0:
            raise RunnerError("max_duration_seconds must be positive")
        if self.max_created_total < 0:
            raise RunnerError("max_created_total must be >= 0")
        if self.pass_limit < 1:
            raise RunnerError("pass_limit must be >= 1")
        if self.pass_timeout_seconds <= 0:
            raise RunnerError("pass_timeout_seconds must be positive")
        if self.pacing_seconds < 0 or self.backoff_cap_seconds < 0:
            raise RunnerError("pacing/backoff must be >= 0")
        if self.max_consecutive_failures < 1:
            raise RunnerError("max_consecutive_failures must be >= 1")
        if self.claim_lease_seconds <= 0:
            raise RunnerError("claim_lease_seconds must be positive")
        if self.max_evaluations_per_pass < 0:
            raise RunnerError("max_evaluations_per_pass must be >= 0")


# Proposed (NOT launched) two-hour timed configuration. Duration and any
# spending approval are separate from implementation approval; cells and
# bboxes below are placeholders the operator must verify before use.
TIMED_TWO_HOUR_PRESET = {
    "preset": "timed-two-hour",
    "policy": "timed",
    "max_duration_seconds": 7200,
    "max_passes": 48,
    "max_created_total": 400,
    "pass_limit": 10,
    "pass_timeout_seconds": 20.0,
    "pacing_seconds": 30.0,
    "backoff_cap_seconds": 300.0,
    "max_consecutive_failures": 5,
    "claim_lease_seconds": 1800.0,
    "cells": "OPERATOR MUST SUPPLY VERIFIED (county, category, bbox) CELLS",
    "launched": False,
}


SABBATH_TZ = "America/Chicago"
SABBATH_START_ENV = "SABBATH_START"
SABBATH_END_ENV = "SABBATH_END"
SABBATH_DEFAULT_START = "fri 18:00"
SABBATH_DEFAULT_END = "sat 20:00"
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _sabbath_endpoint(raw: str, name: str) -> int:
    """Parse 'day HH:MM' into minutes since Monday 00:00 (strict)."""
    parts = str(raw or "").strip().lower().split()
    if len(parts) != 2 or parts[0] not in _WEEKDAYS:
        raise RunnerError(f"{name} must look like 'fri 18:00'")
    clock = parts[1].split(":")
    if len(clock) != 2 or not clock[0].isdigit() or not clock[1].isdigit():
        raise RunnerError(f"{name} must look like 'fri 18:00'")
    hour, minute = int(clock[0]), int(clock[1])
    if hour > 23 or minute > 59:
        raise RunnerError(f"{name} must look like 'fri 18:00'")
    return _WEEKDAYS[parts[0]] * 1440 + hour * 60 + minute


def _sabbath_blackout(now: datetime | None = None) -> bool:
    """Owner-configured Sabbath blackout evaluated in America/Chicago.

    The window is an explicit operator approximation (default Friday
    18:00 through Saturday 20:00 Chicago time), NOT an astronomical
    sundown calculation: the owner adjusts SABBATH_START/SABBATH_END
    for the season rather than trusting a fixed clock time to precede
    sundown year-round. Naive datetimes are interpreted as Chicago
    time. Disable only with explicit SABBATH_BLACKOUT=0. When the
    timezone database is unavailable the guard fails closed (blackout).
    """
    if os.getenv("SABBATH_BLACKOUT", "1").strip() == "0":
        return False
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - stdlib present, data maybe not
        return True
    try:
        chicago = ZoneInfo(SABBATH_TZ)
    except Exception:
        return True
    try:
        start = _sabbath_endpoint(
            os.getenv(SABBATH_START_ENV, SABBATH_DEFAULT_START), SABBATH_START_ENV
        )
        end = _sabbath_endpoint(
            os.getenv(SABBATH_END_ENV, SABBATH_DEFAULT_END), SABBATH_END_ENV
        )
    except RunnerError:
        return True
    current = now or datetime.now(tz=chicago)
    if current.tzinfo is None:
        current = current.replace(tzinfo=chicago)
    else:
        current = current.astimezone(chicago)
    minute = current.weekday() * 1440 + current.hour * 60 + current.minute
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def _is_auth_error(exc: BaseException) -> bool:
    return "auth" in type(exc).__name__.lower()


def _decode_checkpoint(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return dict(value or {})


_RUN_COLUMNS = (
    "run_id, scope, policy, status, config, checkpoint, usage, "
    "created_by, owner, heartbeat_at, stop_requested, error, "
    "started_at, finished_at"
)


def _row_to_run(row: dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    record["run_id"] = str(record.get("run_id"))
    record["config"] = _decode_checkpoint(record.get("config"))
    record["checkpoint"] = _decode_checkpoint(record.get("checkpoint"))
    record["usage"] = _decode_checkpoint(record.get("usage"))
    record["stop_requested"] = bool(record.get("stop_requested", False))
    record["heartbeat_at"] = str(record.get("heartbeat_at", ""))
    record["started_at"] = str(record.get("started_at", ""))
    record["finished_at"] = str(record.get("finished_at", "") or "")
    return record


def claim_work_in_transaction(
    conn: Any,
    *,
    scope: str,
    policy: str,
    checkpoint: dict[str, Any],
    config: dict[str, Any] | None = None,
    created_by: str = "",
    owner: str = "",
) -> dict[str, Any] | None:
    """Claim the single active worker slot for a scope (None if busy).

    The partial unique index serializes concurrent claims: exactly one
    claimant per scope gets a row back. The claim stores the invoker's
    owner token and config bounds for audit and crash recovery.
    """
    if policy not in POLICIES:
        raise RunnerError(f"unknown policy: {policy!r}")
    token = str(owner or "").strip()
    if not token:
        raise RunnerError("owner is required")
    try:
        with conn.cursor() as cur:
            run_id = uuid4()
            cur.execute(
                "INSERT INTO discovery_runs (run_id, scope, policy, status, "
                "config, checkpoint, created_by, owner) "
                "VALUES (%(run)s, %(scope)s, %(policy)s, 'running', "
                "%(config)s, %(checkpoint)s, %(created_by)s, %(owner)s) "
                "ON CONFLICT (scope) WHERE status = 'running' DO NOTHING "
                f"RETURNING {_RUN_COLUMNS}",
                {
                    "run": run_id,
                    "scope": str(scope),
                    "policy": policy,
                    "config": json.dumps(config or {}),
                    "checkpoint": json.dumps(checkpoint or {}),
                    "created_by": str(created_by or ""),
                    "owner": token,
                },
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("discovery_runner_unavailable") from exc
    return _row_to_run(row) if row is not None else None


def adopt_stalled_in_transaction(
    conn: Any,
    run_id: UUID | str,
    owner: str,
    lease_seconds: float,
    *,
    resume: bool = False,
) -> dict[str, Any] | None:
    """Take over a crashed worker's row, or None when adoption is refused.

    Adoption requires the row to still be 'running' with a heartbeat
    older than the lease, evaluated atomically in one UPDATE: racing
    adopters serialize on the row lock and all but one see zero rows.
    A live worker re-heartbeating between our read and this UPDATE is
    never stolen from. Rows flagged stop_requested adopt only under an
    explicit resume, which clears the flag; ordinary starts report the
    stalled-but-stopped row instead of overriding the operator.
    """
    try:
        run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RunnerError("run_id must be a UUID") from exc
    token = str(owner or "").strip()
    if not token:
        raise RunnerError("owner is required")
    if lease_seconds <= 0:
        raise RunnerError("lease_seconds must be positive")
    stop_clause = "" if resume else "AND stop_requested = FALSE "
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET owner = %(owner)s, "
                "heartbeat_at = now(), stop_requested = FALSE, error = NULL "
                "WHERE run_id = %(run)s AND status = 'running' "
                "AND heartbeat_at < now() - make_interval(secs => %(lease)s) "
                f"{stop_clause}"
                f"RETURNING {_RUN_COLUMNS}",
                {"run": run_uuid, "owner": token, "lease": float(lease_seconds)},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("discovery_runner_unavailable") from exc
    return _row_to_run(row) if row is not None else None


def latest_run_in_transaction(conn: Any, scope: str) -> dict[str, Any] | None:
    """Latest worker row for a scope (any status)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_RUN_COLUMNS} FROM discovery_runs "
                "WHERE scope = %(scope)s ORDER BY started_at DESC LIMIT 1",
                {"scope": str(scope)},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("discovery_runner_unavailable") from exc
    return _row_to_run(row) if row is not None else None


def run_status_in_transaction(conn: Any, run_id: UUID | str) -> str | None:
    """Current status of one worker row (stop checks read this fresh)."""
    state = run_state_in_transaction(conn, run_id)
    return state["status"] if state is not None else None


def run_state_in_transaction(conn: Any, run_id: UUID | str) -> dict[str, Any] | None:
    """Current status, stop flag, and owner of one worker row."""
    try:
        run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RunnerError("run_id must be a UUID") from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, stop_requested, owner FROM discovery_runs "
                "WHERE run_id = %(run)s",
                {"run": run_uuid},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("discovery_runner_unavailable") from exc
    if row is None:
        return None
    record = dict(row)
    return {
        "status": str(record.get("status")),
        "stop_requested": bool(record.get("stop_requested", False)),
        "owner": str(record.get("owner", "") or ""),
    }


def checkpoint_in_transaction(
    conn: Any,
    run_id: UUID | str,
    checkpoint: dict[str, Any],
    *,
    owner: str | None = None,
) -> bool:
    """Durably record worker progress; refresh the ownership heartbeat.

    Returns False (writing nothing) when the row is no longer running
    or, with ``owner`` given, no longer owned by this worker — the
    caller must exit instead of continuing under a lost lease.
    """
    try:
        run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RunnerError("run_id must be a UUID") from exc
    owner_clause = "" if owner is None else "AND owner = %(owner)s "
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET checkpoint = %(checkpoint)s, "
                "heartbeat_at = now() "
                "WHERE run_id = %(run)s AND status = 'running' "
                f"{owner_clause}"
                "RETURNING run_id",
                {
                    "checkpoint": json.dumps(checkpoint or {}),
                    "run": run_uuid,
                    "owner": str(owner or ""),
                },
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("discovery_runner_unavailable") from exc
    return row is not None


def finish_run_in_transaction(
    conn: Any,
    run_id: UUID | str,
    status: str,
    error: str | None = None,
    *,
    usage: dict[str, Any] | None = None,
) -> None:
    """Mark this worker's own row finished (completed/aborted/paused)."""
    if status not in ("completed", "aborted", "paused"):
        raise RunnerError(f"bad finish status: {status!r}")
    try:
        run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RunnerError("run_id must be a UUID") from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET status = %(status)s, "
                "finished_at = now(), error = %(error)s, usage = %(usage)s "
                "WHERE run_id = %(run)s",
                {
                    "status": status,
                    "error": error,
                    "run": run_uuid,
                    "usage": json.dumps(usage or {}),
                },
            )
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("discovery_runner_unavailable") from exc


def request_stop_in_transaction(conn: Any, scope: str) -> bool:
    """Ask a running worker to pause between passes (True if one runs).

    The row stays 'running' so the scope slot remains held: no second
    worker can claim it while the owner is still in flight. The owner
    observes the flag at the next pass boundary and marks itself
    paused on exit.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE discovery_runs SET stop_requested = TRUE "
                "WHERE scope = %(scope)s AND status = 'running' "
                "RETURNING run_id",
                {"scope": str(scope)},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("discovery_runner_unavailable") from exc
    return row is not None


def _fresh_checkpoint(cells: list[RunnerCell]) -> dict[str, Any]:
    return {
        "runner_version": RUNNER_VERSION,
        "cursor": 0,
        "passes": 0,
        "created_total": 0,
        "per_cell": {cell.key: 0 for cell in cells},
        "evaluations": {},
        "backlog": [],
        "discovery_runs": [],
        "pending_pass": None,
        "cells": [
            {
                "county": cell.county,
                "category": cell.category,
                "bbox": list(cell.bbox),
                "source": cell.source,
            }
            for cell in cells
        ],
    }


def _cell_order_pilot(
    cells: list[RunnerCell], counts: dict[tuple[str, str], int]
) -> list[RunnerCell]:
    """Unmet cells first (lowest canonical qualified count), stable."""
    return sorted(
        cells,
        key=lambda cell: (
            counts.get((cell.county, cell.category), 0),
            cells.index(cell),
        ),
    )


def _cell_order_timed(
    cells: list[RunnerCell], checkpoint: dict[str, Any]
) -> list[RunnerCell]:
    """Round-robin by least created this campaign, stable."""
    per_cell = checkpoint.get("per_cell") or {}
    return sorted(
        cells,
        key=lambda cell: (int(per_cell.get(cell.key, 0)), cells.index(cell)),
    )


def _quota_met(counts: dict[tuple[str, str], int]) -> bool:
    from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER
    from app.pilot import PILOT_CATEGORIES, TARGET_QUALIFIED_PER_CELL

    for county in COUNTY_PRIORITY_ORDER:
        for category in PILOT_CATEGORIES:
            if counts.get((county, category), 0) < TARGET_QUALIFIED_PER_CELL:
                return False
    return True


def _config_bounds(config: RunnerConfig) -> dict[str, Any]:
    """Persisted snapshot of one invocation's bounds (audit + recovery)."""
    return {
        "runner_version": RUNNER_VERSION,
        "policy": config.policy,
        "max_passes": config.max_passes,
        "max_duration_seconds": config.max_duration_seconds,
        "max_created_total": config.max_created_total,
        "pass_limit": config.pass_limit,
        "pass_timeout_seconds": config.pass_timeout_seconds,
        "pacing_seconds": config.pacing_seconds,
        "backoff_cap_seconds": config.backoff_cap_seconds,
        "max_consecutive_failures": config.max_consecutive_failures,
        "claim_lease_seconds": config.claim_lease_seconds,
        "acquire_approved": config.acquire_approved,
        "max_evaluations_per_pass": config.max_evaluations_per_pass,
    }


def _merge_saved_checkpoint(
    config: RunnerConfig, saved: dict[str, Any] | None
) -> dict[str, Any]:
    """Fresh checkpoint carrying forward durable progress for these cells."""
    checkpoint = _fresh_checkpoint(config.cells)
    if not saved:
        return checkpoint
    checkpoint["created_total"] = int(saved.get("created_total", 0))
    checkpoint["passes"] = int(saved.get("passes", 0))
    checkpoint["cursor"] = int(saved.get("cursor", 0))
    for key, value in (saved.get("per_cell") or {}).items():
        if key in checkpoint["per_cell"]:
            checkpoint["per_cell"][key] = int(value)
    saved_evals = saved.get("evaluations") or {}
    if isinstance(saved_evals, dict):
        checkpoint["evaluations"] = {
            str(key): dict(value) for key, value in saved_evals.items()
            if isinstance(value, dict)
        }
    terminal = {
        pid for pid, prior in checkpoint["evaluations"].items()
        if _is_terminal_outcome(prior)
    }
    seen: set[str] = set()
    for pid in saved.get("backlog") or []:
        pid = str(pid)
        if pid in terminal or pid in seen:
            continue
        seen.add(pid)
        checkpoint["backlog"].append(pid)
    runs = saved.get("discovery_runs") or []
    if isinstance(runs, list):
        checkpoint["discovery_runs"] = [str(run) for run in runs[-200:]]
    pending = saved.get("pending_pass")
    if isinstance(pending, dict) and str(pending.get("discovery_run_id", "") or ""):
        checkpoint["pending_pass"] = {
            "seq": pending.get("seq", 0),
            "discovery_run_id": str(pending["discovery_run_id"]),
            "cell": str(pending.get("cell", "") or ""),
        }
    return checkpoint


def run_is_stale_in_transaction(
    conn: Any, run_id: UUID | str, lease_seconds: float
) -> bool | None:
    """True when a running row's heartbeat is older than the lease.

    None when the row is missing or no longer running. The lease must
    exceed any single pass: a slow-but-live worker must never read as
    stale mid-pass.
    """
    try:
        run_uuid = run_id if isinstance(run_id, UUID) else UUID(str(run_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise RunnerError("run_id must be a UUID") from exc
    if lease_seconds <= 0:
        raise RunnerError("lease_seconds must be positive")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (heartbeat_at < now() - make_interval(secs => %(lease)s)) "
                "AS stale FROM discovery_runs "
                "WHERE run_id = %(run)s AND status = 'running'",
                {"run": run_uuid, "lease": float(lease_seconds)},
            )
            row = cur.fetchone()
    except _DRIVER_ERRORS as exc:
        raise DatabaseUnavailableError("discovery_runner_unavailable") from exc
    return None if row is None else bool(dict(row).get("stale", False))


def _adopt_or_busy(
    conn: Any,
    config: RunnerConfig,
    previous: dict[str, Any],
    owner: str,
    report: dict[str, Any],
) -> dict[str, Any] | None:
    """Adopt a crashed worker's row, or explain why this burst waits.

    Returns the adopted row (flagged ``adopted``) or None. A live row
    means busy; a stale row of another policy aborts this invocation
    rather than mixing policies; a stale stopped row adopts only on
    explicit resume. A lost adoption race means busy.
    """
    stale = run_is_stale_in_transaction(
        conn, previous["run_id"], config.claim_lease_seconds
    )
    if stale is not True:
        return None
    if str(previous.get("policy", "")) != config.policy:
        report.update(
            outcome="aborted",
            reason=(
                "stalled worker row uses policy "
                f"{previous.get('policy', '')!r}; resolve it before running "
                f"{config.policy!r} on this scope"
            ),
        )
        return None
    if previous.get("stop_requested") and not config.resume:
        report.update(
            outcome="paused",
            reason="previous worker ended after a stop request; resume explicitly",
        )
        return None
    adopted = adopt_stalled_in_transaction(
        conn, previous["run_id"], owner, config.claim_lease_seconds,
        resume=config.resume,
    )
    if adopted is None:
        return None
    adopted["adopted"] = True
    adopted["previous_owner"] = str(previous.get("owner", "") or "")
    return adopted


TERMINAL_EVAL_OUTCOMES = frozenset({"evaluated", "blocked", "skipped"})
_NO_RETRY_MARKERS = (
    "retry requires a new assignment id",
    "ambiguous prior attempt",
)


def _is_terminal_outcome(prior: Any) -> bool:
    return (
        isinstance(prior, dict)
        and prior.get("outcome") in TERMINAL_EVAL_OUTCOMES
    )


def _pass_run_id(scope: str, run_id: UUID, seq: int) -> str:
    """Deterministic discovery attribution for one runner pass.

    Saved in the checkpoint *before* discovery runs and carried by
    every candidate decision event the pass commits, so recovery can
    name the exact interrupted pass without fuzzy adoption. Bounded
    to the event store's 128-char run_id limit.
    """
    short = str(run_id).replace("-", "")[:8]
    safe = "".join(
        ch if ch.isalnum() or ch in "-_." else "-"
        for ch in str(scope)
    )[:64]
    return f"runner:{safe or 'scope'}:{short}:{int(seq)}"


def _backlog_depth(checkpoint: dict[str, Any]) -> int:
    """Queued ids without a terminal recorded outcome."""
    backlog = checkpoint.get("backlog") or []
    evaluations = checkpoint.get("evaluations") or {}
    if not isinstance(backlog, list) or not isinstance(evaluations, dict):
        return 0
    return sum(
        1 for pid in backlog
        if not _is_terminal_outcome(evaluations.get(str(pid)))
    )


def _recover_pending_pass(
    checkpoint: dict[str, Any],
    *,
    events_fn: Callable[[str], list[Any]] | None = None,
) -> dict[str, Any]:
    """Fold an interrupted pass's committed candidates into the backlog.

    Reads the exact discovery run's permanent decision events
    (database reads only — no provider calls, no property creation)
    and enqueues committed creations. Only ``created`` outcomes with
    a COMMITTED mutation and a valid property UUID count; skips,
    summaries, and malformed rows are ignored, never adopted.
    Raises DatabaseUnavailableError when the event store cannot be
    read (the caller must abort rather than proceed blind).
    """
    from app.agents.gabriel.service import (
        DECISION_EVENT_TYPE,
        MUTATION_COMMITTED,
    )

    pending = checkpoint.get("pending_pass") or {}
    run_id = str(pending.get("discovery_run_id", "") or "")
    folded = checkpoint.get("discovery_runs") or []
    if not run_id or run_id in folded:
        checkpoint["pending_pass"] = None
        return {"discovery_run_id": run_id, "enqueued": 0, "already_folded": True}
    if events_fn is None:
        from app.observability.store import by_run as events_fn
    created_ids: list[str] = []
    seen: set[str] = set()
    for event in events_fn(run_id) or []:
        if getattr(event, "event_type", "") != DECISION_EVENT_TYPE:
            continue
        details = getattr(event, "details", None) or {}
        if not isinstance(details, dict):
            continue
        if details.get("outcome") != "created":
            continue
        if details.get("mutation_status") != MUTATION_COMMITTED:
            continue
        result = details.get("result") or {}
        pid = ""
        if isinstance(result, dict):
            pid = str(result.get("property_id", "") or "")
        try:
            UUID(pid)
        except (ValueError, AttributeError, TypeError):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        created_ids.append(pid)
    _enqueue_backlog(checkpoint, created_ids, run_id)
    checkpoint["pending_pass"] = None
    return {"discovery_run_id": run_id, "enqueued": len(created_ids),
            "already_folded": False}


def _enqueue_backlog(
    checkpoint: dict[str, Any],
    created_ids: list[str],
    discovery_run_id: str = "",
) -> int:
    """Persist every discovered candidate before per-pass caps apply.

    Appends ids that are neither already queued nor terminally
    recorded, preserving discovery order. Returns the backlog depth.
    The caller saves the checkpoint immediately after, so candidates
    survive even if the evaluations below crash or the burst ends.
    """
    backlog = checkpoint.setdefault("backlog", [])
    if not isinstance(backlog, list):
        backlog = checkpoint["backlog"] = []
    evaluations = checkpoint.setdefault("evaluations", {})
    if not isinstance(evaluations, dict):
        evaluations = checkpoint["evaluations"] = {}
    queued = {str(pid) for pid in backlog}
    for pid in created_ids:
        pid = str(pid)
        if pid in queued or _is_terminal_outcome(evaluations.get(pid)):
            continue
        queued.add(pid)
        backlog.append(pid)
    run_id = str(discovery_run_id or "")
    if run_id:
        runs = checkpoint.setdefault("discovery_runs", [])
        if isinstance(runs, list) and run_id not in runs:
            runs.append(run_id)
            del runs[:-200]
    return len(backlog)


def build_evaluation_query(prop: dict[str, Any]) -> str | None:
    """Deterministic acquisition query from a stored property address.

    Returns None when no usable address exists; the caller records a
    skip rather than inventing a query.
    """
    street = str(prop.get("street_address") or "").strip()
    city = str(prop.get("city") or "").strip()
    if not street or not city:
        return None
    query = f"{street}, {city}"
    state = str(prop.get("state") or "").strip()
    if state:
        query += f", {state}"
    zip_code = str(prop.get("zip_code") or "").strip()
    if zip_code:
        query += f" {zip_code}"
    return f"{query} commercial property"


def evaluate_property(
    scope: str, property_id: str, *, approved: bool, timeout_seconds: float
) -> dict[str, Any]:
    """Drive one discovered property through the existing coordinator.

    Uses the property's own operational identity end to end (one
    pipeline): a deterministic assignment derived from that identity,
    acquisition through the coordinator's budget/ambiguity guards,
    dossier persistence, qualification, and Raphael review. Outcomes:

    - ``evaluated``: coordinator summary with decision + qualified count.
    - ``blocked``: coordinator refused (ambiguous prior attempt or
      similar no-retry signal); terminal, never retried blindly.
    - ``skipped``: property-specific dead end (missing row, no usable
      address, invalid identity); terminal.
    - ``error``: anything else went wrong; re-driven on resume.

    Database outages propagate so the burst aborts loudly.
    """
    if not approved:
        raise RunnerError("property evaluation requires acquisition approval")
    pid = str(property_id or "").strip()
    try:
        target = UUID(pid)
    except (ValueError, AttributeError, TypeError):
        return {"outcome": "skipped", "reason": "invalid property identity"}
    from app.mvp import start_evaluation_audited
    from app.properties_db import get_property
    from app.workflow_db import WorkflowError

    prop = get_property(target)
    if prop is None:
        return {"outcome": "skipped", "reason": "property row missing"}
    query = build_evaluation_query(prop)
    if query is None:
        return {"outcome": "skipped", "reason": "no usable address"}
    try:
        summary = start_evaluation_audited(
            f"runner:{scope}:{pid}", query,
            approved=True, timeout_seconds=timeout_seconds, property_id=pid,
        )
    except WorkflowError as exc:
        message = str(exc)
        if any(marker in message for marker in _NO_RETRY_MARKERS):
            return {"outcome": "blocked", "reason": message}
        return {"outcome": "error", "reason": message}
    qualification = summary.get("qualification") or {}
    dossiers = len(qualification) if isinstance(qualification, dict) else 0
    qualified = (
        sum(1 for value in qualification.values() if value == "qualified")
        if isinstance(qualification, dict)
        else 0
    )
    return {
        "outcome": "evaluated",
        "run_id": str(summary.get("run_id", "")),
        "decision": str(summary.get("decision", "")),
        "dossiers": dossiers,
        "qualified": qualified,
    }


def _coordinator_enabled() -> bool:
    from app.mvp import workflow_enabled

    return workflow_enabled()


def _drive_evaluations(
    evaluate_fn: Callable[[str, str], dict[str, Any]],
    config: RunnerConfig,
    checkpoint: dict[str, Any],
    report: dict[str, Any],
) -> tuple[int, int] | None:
    """Drive the durable backlog head through the coordinator (one pass).

    The backlog (persisted before caps apply) is the only work
    source: resume never depends on a candidate reappearing in a
    later pass's ``created_ids``. Terminal outcomes leave the
    backlog; ``error`` outcomes stay queued for resume. A crashed
    in-flight evaluation resumes under its deterministic assignment,
    where the coordinator either replays the completed artifact or
    refuses an ambiguous attempt — never blindly re-running a
    possibly-charged call. Returns (evaluated, qualified) or None
    on database outage (backlog intact for the finish-path re-save).
    """
    backlog = checkpoint.setdefault("backlog", [])
    if not isinstance(backlog, list):
        backlog = checkpoint["backlog"] = []
    evaluations = checkpoint.setdefault("evaluations", {})
    backlog[:] = [
        pid for pid in backlog
        if not _is_terminal_outcome(evaluations.get(str(pid)))
    ]
    driven = 0
    evaluated = 0
    qualified = 0
    for pid in list(backlog):
        if driven >= config.max_evaluations_per_pass:
            break
        try:
            outcome = evaluate_fn(config.scope, str(pid))
        except (DatabaseUnavailableError, DatabaseNotConfiguredError):
            return None
        if not isinstance(outcome, dict) or not outcome.get("outcome"):
            outcome = {"outcome": "error", "reason": "empty evaluation outcome"}
        evaluations[str(pid)] = outcome
        driven += 1
        if outcome.get("outcome") in TERMINAL_EVAL_OUTCOMES:
            try:
                backlog.remove(pid)
            except ValueError:
                pass
        if outcome.get("outcome") == "evaluated":
            evaluated += 1
            try:
                qualified += int(outcome.get("qualified", 0) or 0)
            except (TypeError, ValueError):
                pass
    report["evaluations_pending"] = len(backlog)
    return evaluated, qualified


def run_worker(
    get_conn: Callable[[], Any],
    config: RunnerConfig,
    *,
    discovery_fn: Callable[..., Any] | None = None,
    counts_fn: Callable[[], dict[tuple[str, str], int]] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    clock_fn: Callable[[], float] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    evaluate_fn: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute one bounded worker burst, checkpointing every pass.

    ``get_conn`` supplies short-lived connections the worker commits
    and closes itself. ``discovery_fn`` defaults to the real
    ``run_discovery``; tests inject scripted doubles. Returns a
    plain-JSON report; never raises for provider failures (they
    abort the burst with a recorded reason instead).

    Ownership: exactly one worker holds a scope. A stop request never
    releases the slot early, and crash recovery adopts only rows
    whose heartbeat expired under the configured lease — and only
    ever the same policy, so a timed burst cannot silently resume a
    pilot row or vice versa.

    Each pass stamps a deterministic discovery run id, saves it as
    the checkpoint's pending pass *before* discovery runs, then
    enqueues every created id in a durable backlog (saved before
    any per-pass cap applies) and drives the backlog head through
    the existing coordinator (``evaluate_fn``, default
    :func:`evaluate_property`) when acquisition is approved and
    the coordinator is enabled. Resume works from the backlog
    alone — never from a candidate reappearing in a later pass —
    and a pass interrupted between discovery's commit and the
    checkpoint save is recovered exactly from its permanent
    decision events (see :func:`_recover_pending_pass`).
    Discovery limits (creation ceiling, pilot quota) stop new
    discovery only: a pending backlog keeps draining in bounded
    rounds under the same duration/stop/evaluation limits.
    Reported ``created`` counts property records; only coordinator
    ``qualified`` outcomes count toward dossier qualification —
    the two are never substituted.
    """
    config.validate()
    sleep = sleep_fn or time.sleep
    clock = clock_fn or time.monotonic
    if discovery_fn is None:
        from app.agents.gabriel.schemas import BoundingBoxInput, DiscoveryRunRequest
        from app.agents.gabriel.service import run_discovery

        def _real(cell: RunnerCell, run_id: str = "") -> Any:
            south, west, north, east = cell.bbox
            return run_discovery(
                DiscoveryRunRequest(
                    source=cell.source,
                    bbox=BoundingBoxInput(
                        south=south, west=west, north=north, east=east
                    ),
                    categories=[cell.category],
                    limit=config.pass_limit,
                    timeout_seconds=config.pass_timeout_seconds,
                    run_id=run_id or None,
                )
            )

        discovery_fn = _real
    if counts_fn is None:
        from app.pilot import canonical_qualified_counts

        counts_fn = canonical_qualified_counts

    if evaluate_fn is None:
        if not config.acquire_approved:
            evaluations_precondition: str | None = "acquisition not approved"
        elif config.max_evaluations_per_pass <= 0:
            evaluations_precondition = "evaluations disabled by bound"
        elif not _coordinator_enabled():
            evaluations_precondition = "coordinator disabled"
        else:
            evaluations_precondition = None
            approved = config.acquire_approved
            timeout = config.pass_timeout_seconds

            def _default_evaluate(scope: str, pid: str) -> dict[str, Any]:
                return evaluate_property(
                    scope, pid, approved=approved,
                    timeout_seconds=timeout,
                )

            evaluate_fn = _default_evaluate
    else:
        evaluations_precondition = None
    report: dict[str, Any] = {
        "scope": config.scope,
        "policy": config.policy,
        "run_id": None,
        "outcome": "started",
        "passes": 0,
        "created": 0,
        "evaluated": 0,
        "qualified_dossiers": 0,
        "evaluations_precondition": evaluations_precondition,
        "recovered_pending_pass": None,
        "reason": None,
    }
    if _sabbath_blackout(now_fn() if now_fn else None):
        report.update(outcome="paused", reason="Sabbath blackout window")
        return report
    owner = f"{config.created_by or 'worker'}:{uuid4().hex[:8]}"

    def _connect() -> Any:
        try:
            return get_conn()
        except _DRIVER_ERRORS as exc:
            raise DatabaseUnavailableError("discovery_runner_unavailable") from exc

    def _close(conn: Any) -> None:
        try:
            conn.close()
        except Exception:
            pass

    conn = _connect()
    try:
        previous = latest_run_in_transaction(conn, config.scope)
        checkpoint = _merge_saved_checkpoint(
            config, dict(previous["checkpoint"]) if previous else None
        )
        claimed = claim_work_in_transaction(
            conn, scope=config.scope, policy=config.policy,
            checkpoint=checkpoint, config=_config_bounds(config),
            created_by=config.created_by, owner=owner,
        )
        if claimed is None and previous is not None and previous.get("status") == "running":
            claimed = _adopt_or_busy(conn, config, previous, owner, report)
        if claimed is None and report.get("outcome") == "started":
            # Winner may have finished between our read and claim; retry once.
            claimed = claim_work_in_transaction(
                conn, scope=config.scope, policy=config.policy,
                checkpoint=checkpoint, config=_config_bounds(config),
                created_by=config.created_by, owner=owner,
            )
        # Adopted rows resume from `previous`, which the merge above
        # already folded in (same row, same checkpoint).
        conn.commit()
    except DatabaseUnavailableError:
        _close(conn)
        report.update(outcome="aborted", reason="database unavailable at claim")
        return report
    if claimed is None:
        _close(conn)
        if report.get("outcome") == "started":
            report.update(outcome="busy", reason="another worker holds this scope")
        return report
    _close(conn)

    run_id = claimed["run_id"]
    report["run_id"] = run_id
    if claimed.get("adopted"):
        report["adopted_from"] = claimed.get("previous_owner", "")
    started = clock()
    consecutive_failures = 0
    burst_passes = 0
    burst_created = 0
    burst_evaluated = 0
    burst_qualified = 0

    def _usage() -> dict[str, Any]:
        return {
            "runner_version": RUNNER_VERSION,
            "passes": burst_passes,
            "created": burst_created,
            "evaluated": burst_evaluated,
            "qualified_dossiers": burst_qualified,
        }

    def _tally() -> None:
        report["passes"] = burst_passes
        report["created"] = burst_created
        report["evaluated"] = burst_evaluated
        report["qualified_dossiers"] = burst_qualified

    def _finish(status: str, reason: str | None) -> dict[str, Any]:
        report.update(outcome=status, reason=reason)
        end = _connect()
        try:
            owned = checkpoint_in_transaction(end, run_id, checkpoint, owner=owner)
            if not owned:
                report.update(
                    outcome="paused",
                    reason="worker ownership lost; adopted by recovery",
                )
                end.commit()
            else:
                finish_run_in_transaction(end, run_id, status, reason, usage=_usage())
                end.commit()
        except DatabaseUnavailableError:
            try:
                end.rollback()
            except Exception:
                pass
            report["checkpoint_durable"] = False
        else:
            report["checkpoint_durable"] = True
        finally:
            _close(end)
        _tally()
        return report

    def _persist_checkpoint() -> bool:
        """Save the checkpoint; finish and False when it cannot be saved.

        A failed mid-pass save aborts through ``_finish``, which
        re-attempts the save with the in-memory checkpoint — the
        in-process recovery path for discovery committed before
        checkpoint persistence. Cross-process crashes in that window
        are covered by pending-pass attribution instead (see
        :func:`_recover_pending_pass`): the backlog survives and is
        re-saved, or is honestly reported non-durable.
        """
        save_conn = _connect()
        try:
            owned = checkpoint_in_transaction(
                save_conn, run_id, checkpoint, owner=owner
            )
            save_conn.commit()
        except DatabaseUnavailableError:
            _close(save_conn)
            _finish("aborted", "checkpoint write failed")
            return False
        _close(save_conn)
        if not owned:
            report.update(
                outcome="paused",
                reason="worker ownership lost; adopted by recovery",
            )
            _tally()
            return False
        return True

    if checkpoint.get("pending_pass"):
        # Crash recovery: fold the interrupted pass's committed
        # candidates (exact run-id match; event-store reads only).
        try:
            recovered = _recover_pending_pass(checkpoint)
        except (DatabaseUnavailableError, DatabaseNotConfiguredError):
            return _finish("aborted", "pending-pass recovery unavailable")
        report["recovered_pending_pass"] = recovered
        if not _persist_checkpoint():
            return report

    while True:
        if _sabbath_blackout(now_fn() if now_fn else None):
            return _finish("paused", "Sabbath blackout window")
        if clock() - started >= config.max_duration_seconds:
            return _finish("paused", "duration limit reached")
        if burst_passes >= config.max_passes:
            return _finish("completed", "pass budget exhausted")
        status_conn = _connect()
        try:
            state = run_state_in_transaction(status_conn, run_id)
        finally:
            _close(status_conn)
        if state is None or state["status"] != "running" or state["owner"] != owner:
            report.update(outcome="paused", reason="worker ownership lost; adopted by recovery")
            _tally()
            return report
        if state["stop_requested"]:
            return _finish("paused", "operator stop observed")
        try:
            counts = counts_fn()
        except Exception as exc:  # quota reads must not invent progress
            return _finish("aborted", f"pilot counts unreadable: {type(exc).__name__}")
        ceiling_hit = checkpoint["created_total"] >= config.max_created_total
        quota_done = config.policy == "pilot" and _quota_met(counts)
        if ceiling_hit or quota_done:
            # Discovery limits stop *new* discovery; the durable
            # backlog stays reachable and drains under the same
            # duration/stop/evaluation bounds. Each drain round
            # consumes one pass of the burst budget.
            reason = (
                "creation budget exhausted"
                if ceiling_hit
                else "pilot quota satisfied"
            )
            if evaluate_fn is None or _backlog_depth(checkpoint) == 0:
                return _finish("completed", reason)
            driven = _drive_evaluations(evaluate_fn, config, checkpoint, report)
            if driven is None:  # database outage inside the coordinator
                return _finish("aborted", "evaluation database unavailable")
            burst_evaluated += driven[0]
            burst_qualified += driven[1]
            burst_passes += 1
            if not _persist_checkpoint():
                return report
            sleep(config.pacing_seconds)
            continue
        ordered = (
            _cell_order_pilot(config.cells, counts)
            if config.policy == "pilot"
            else _cell_order_timed(config.cells, checkpoint)
        )
        cell = ordered[checkpoint["cursor"] % len(ordered)]
        pending_id = _pass_run_id(config.scope, run_id, checkpoint["passes"])
        checkpoint["pending_pass"] = {
            "seq": checkpoint["passes"],
            "discovery_run_id": pending_id,
            "cell": cell.key,
        }
        if not _persist_checkpoint():
            return report
        try:
            result = discovery_fn(cell, pending_id)
            created = int(getattr(result.counts, "created", 0))
        except Exception as exc:
            consecutive_failures += 1
            if _is_auth_error(exc) or type(exc).__name__ in (
                "DatabaseNotConfiguredError",
                "DatabaseUnavailableError",
            ):
                return _finish("aborted", f"auth/infra failure: {type(exc).__name__}")
            if consecutive_failures >= config.max_consecutive_failures:
                return _finish(
                    "aborted",
                    f"{consecutive_failures} consecutive provider failures "
                    f"({type(exc).__name__})",
                )
            backoff = min(
                config.pacing_seconds * (2 ** (consecutive_failures - 1)),
                config.backoff_cap_seconds,
            )
            sleep(backoff)
            continue
        consecutive_failures = 0
        burst_passes += 1
        burst_created += created
        checkpoint["passes"] = int(checkpoint["passes"]) + 1
        checkpoint["created_total"] = int(checkpoint["created_total"]) + created
        checkpoint["per_cell"][cell.key] = int(checkpoint["per_cell"][cell.key]) + created
        checkpoint["cursor"] = int(checkpoint["cursor"]) + 1
        checkpoint["last_cell"] = cell.key
        checkpoint["last_pass_created"] = created
        try:
            pass_ids = [str(i) for i in (getattr(result, "created_ids", None) or [])]
        except TypeError:
            pass_ids = []
        _enqueue_backlog(checkpoint, pass_ids, getattr(result, "run_id", "") or "")
        checkpoint["pending_pass"] = None
        if not _persist_checkpoint():
            return report
        if evaluate_fn is not None:
            driven = _drive_evaluations(evaluate_fn, config, checkpoint, report)
            if driven is None:  # database outage inside the coordinator
                return _finish("aborted", "evaluation database unavailable")
            burst_evaluated += driven[0]
            burst_qualified += driven[1]
            if not _persist_checkpoint():
                return report
        sleep(config.pacing_seconds)


def _cells_from_json(path: str) -> list[RunnerCell]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, list) or not raw:
        raise RunnerError("cells file must be a non-empty JSON list")
    cells = []
    for item in raw:
        if not isinstance(item, dict):
            raise RunnerError("each cell must be an object")
        bbox = item.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, (int, float)) for value in bbox)
        ):
            raise RunnerError("each cell needs bbox [south, west, north, east]")
        cells.append(
            RunnerCell(
                county=str(item.get("county", "")),
                category=str(item.get("category", "")),
                bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                source=str(item.get("source", "overpass") or "overpass"),
            )
        )
    return cells


def main(argv: list[str] | None = None) -> int:
    """CLI: run|status|stop. ``run`` executes one bounded burst only."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("run", "status", "stop"):
        print(
            "usage: python -m app.discovery_runner run|status|stop "
            "--scope SCOPE [--policy pilot|timed] [--cells FILE] "
            "[--max-passes N] [--max-duration SECONDS] [--max-created N] "
            "[--resume] [--created-by LABEL] [--acquire-approved] "
            "[--max-evaluations N]",
            file=sys.stderr,
        )
        return 1
    command = args[0]
    opts = args[1:]

    def _opt(name: str, default: str | None = None) -> str | None:
        if name in opts:
            pos = opts.index(name)
            if pos + 1 >= len(opts):
                raise RunnerError(f"{name} needs a value")
            return opts[pos + 1]
        return default

    from app.properties_db import get_connection

    def _connect() -> Any:
        conn = get_connection()
        conn.autocommit = False
        return conn

    try:
        scope = _opt("--scope") or ""
        if command == "status":
            if not scope:
                raise RunnerError("--scope is required")
            conn = _connect()
            try:
                row = latest_run_in_transaction(conn, scope)
            finally:
                conn.close()
            print(json.dumps(row or {"scope": scope, "run": None}, default=str))
            return 0
        if command == "stop":
            if not scope:
                raise RunnerError("--scope is required")
            conn = _connect()
            try:
                stopped = request_stop_in_transaction(conn, scope)
                conn.commit()
            finally:
                conn.close()
            print(json.dumps({"scope": scope, "stop_requested": stopped}))
            return 0
        cells_path = _opt("--cells") or ""
        if not scope or not cells_path:
            raise RunnerError("--scope and --cells are required for run")
        config = RunnerConfig(
            scope=scope,
            policy=_opt("--policy", "pilot") or "pilot",
            cells=_cells_from_json(cells_path),
            max_passes=int(_opt("--max-passes", "1") or "1"),
            max_duration_seconds=float(_opt("--max-duration", "600") or "600"),
            max_created_total=int(_opt("--max-created", "50") or "50"),
            resume="--resume" in opts,
            created_by=_opt("--created-by", "cli") or "cli",
            acquire_approved="--acquire-approved" in opts,
            max_evaluations_per_pass=int(_opt("--max-evaluations", "5") or "5"),
        )
        report = run_worker(_connect, config)
        print(json.dumps(report, default=str))
        return 0 if report.get("outcome") in ("completed", "paused", "busy") else 1
    except (RunnerError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
