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

Stop, duration, Sabbath, and request bounds apply between work
units (passes and evaluations), never mid-write; an in-flight
provider request cannot be cancelled and runs to its own
timeout. Discovery advances through configured search units with
durable exhaustion (a pass that creates nothing exhausts its
exact query); fulfilled pilot cells are skipped under the pilot
policy. Every pass carries an effective limit clamped to the
remaining creation allowance, and the report states per-provider
request bounds alongside actual spend.
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

    @property
    def unit_key(self) -> str:
        """Durable search-unit identity: pilot cell + exact window + source.

        ``key`` names the pilot cell for quota accounting; ``unit_key``
        names one configured search window for progression state, so
        approved geographic subdivisions (same county/category,
        different bboxes) progress and exhaust independently.
        """
        south, west, north, east = self.bbox
        return (
            f"{self.county}||{self.category}||"
            f"{south!r},{west!r},{north!r},{east!r}||{self.source}"
        )


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
    evaluation_attempt_limit: int = 9
    evaluation_timeout_seconds: float | None = None
    max_overpass_requests: int | None = None
    max_created_per_unit: int | None = None
    max_discovery_attempts_per_unit: int | None = None
    max_census_requests: int | None = None
    census_attempt_limit: int = 1

    @property
    def overpass_request_bound(self) -> int:
        """Burst Overpass request budget (defaults to one per pass)."""
        if self.max_overpass_requests is None:
            return self.max_passes
        return self.max_overpass_requests

    @property
    def census_request_bound(self) -> int:
        """Burst Census request budget (disabled unless explicitly set).

        Unlike Overpass, Census has no per-pass default: county
        admission transmits only inside an explicitly bounded
        envelope.
        """
        if self.max_census_requests is None:
            return 0
        return self.max_census_requests

    def validate(self) -> None:
        from app.pilot import normalize_category, normalize_county

        if not str(self.scope or "").strip():
            raise RunnerError("scope is required")
        if self.policy not in POLICIES:
            raise RunnerError(f"unknown policy: {self.policy!r}")
        if not self.cells:
            raise RunnerError("at least one cell is required")
        for cell in self.cells:
            try:
                normalize_county(cell.county)
                normalize_category(cell.category)
            except ValueError as exc:
                raise RunnerError(f"unknown pilot cell: {cell.key} ({exc})") from exc
            for value in cell.bbox:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RunnerError(f"cell bbox must hold numbers: {cell.key}")
                if value != value or value in (float("inf"), float("-inf")):
                    raise RunnerError(f"cell bbox must be finite: {cell.key}")
            south, west, north, east = cell.bbox
            if not (south < north and west < east):
                raise RunnerError(
                    f"cell bbox must order south<north, west<east: {cell.key}"
                )
            if not str(cell.source or "").strip():
                raise RunnerError(f"cell source is required: {cell.key}")
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
        if (
            isinstance(self.evaluation_attempt_limit, bool)
            or not isinstance(self.evaluation_attempt_limit, int)
            or self.evaluation_attempt_limit < 1
            or self.evaluation_attempt_limit > 18
        ):
            raise RunnerError("evaluation_attempt_limit must be an integer 1..18")
        if self.evaluation_timeout_seconds is not None and (
            isinstance(self.evaluation_timeout_seconds, bool)
            or not isinstance(self.evaluation_timeout_seconds, (int, float))
            or self.evaluation_timeout_seconds <= 0
            or self.evaluation_timeout_seconds > 300
        ):
            raise RunnerError("evaluation_timeout_seconds must be within (0, 300]")
        if self.max_overpass_requests is not None and (
            isinstance(self.max_overpass_requests, bool)
            or not isinstance(self.max_overpass_requests, int)
            or self.max_overpass_requests < 1
        ):
            raise RunnerError("max_overpass_requests must be an integer >= 1")
        if self.max_created_per_unit is not None and (
            isinstance(self.max_created_per_unit, bool)
            or not isinstance(self.max_created_per_unit, int)
            or self.max_created_per_unit < 1
        ):
            raise RunnerError("max_created_per_unit must be an integer >= 1")
        if self.max_discovery_attempts_per_unit is not None and (
            isinstance(self.max_discovery_attempts_per_unit, bool)
            or not isinstance(self.max_discovery_attempts_per_unit, int)
            or self.max_discovery_attempts_per_unit < 1
        ):
            raise RunnerError(
                "max_discovery_attempts_per_unit must be an integer >= 1"
            )
        if self.max_census_requests is not None and (
            isinstance(self.max_census_requests, bool)
            or not isinstance(self.max_census_requests, int)
            or self.max_census_requests < 1
        ):
            raise RunnerError("max_census_requests must be an integer >= 1")
        if (
            isinstance(self.census_attempt_limit, bool)
            or not isinstance(self.census_attempt_limit, int)
            or self.census_attempt_limit < 0
            or self.census_attempt_limit > 1
        ):
            raise RunnerError("census_attempt_limit must be an integer 0..1")

    @property
    def evaluation_timeout(self) -> float:
        """Provider timeout for coordinator evaluations.

        An explicit evaluation timeout wins; otherwise evaluations
        share the pass timeout (ordinary behavior).
        """
        if self.evaluation_timeout_seconds is not None:
            return float(self.evaluation_timeout_seconds)
        return float(self.pass_timeout_seconds)


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
    """Ask a running worker to pause between work units (True if one runs).

    The row stays 'running' so the scope slot remains held: no second
    worker can claim it while the owner is still in flight. The owner
    observes the flag at the next pass or evaluation boundary and
    marks itself paused on exit.
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
        "cell_state": {},
        "overpass_requests": 0,
        "brave_requests": 0,
        "census_requests": 0,
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


def _canonical_cell_key(cell: RunnerCell) -> tuple[str, str]:
    """Pilot-cell identity in canonical form for ledger lookups."""
    from app.pilot import normalize_category, normalize_county

    return (normalize_county(cell.county), normalize_category(cell.category))


def _cell_order_pilot(
    cells: list[RunnerCell], counts: dict[tuple[str, str], int]
) -> list[RunnerCell]:
    """Unmet cells first (lowest canonical qualified count), stable."""
    return sorted(
        cells,
        key=lambda cell: (
            counts.get(_canonical_cell_key(cell), 0),
            cells.index(cell),
        ),
    )


def burst_request_bounds(config: RunnerConfig) -> dict[str, int]:
    """Stated per-provider request maxima for one approved burst.

    Overpass: one request per discovery call against
    ``overpass_request_bound`` (retries share the budget). Brave:
    at most ``max_passes`` drive rounds of
    ``max_evaluations_per_pass`` attempts at ``evaluation_attempt_limit``
    each — every drive round is paired with exactly one consumed
    pass. A resumed run keeps its persisted (never widened) limit,
    which may exceed the configured figure for that one run, so
    the worker enforces the bound by halting further evaluations
    once burst spend reaches it rather than assuming per-attempt
    exactness. Census: at most ``census_request_bound`` address
    requests per burst (0 unless explicitly set) and at most
    ``census_attempt_limit`` per evaluated property; evaluations
    past the burst budget run without county admission rather
    than transmitting. In-flight requests run to their own
    timeout and may land just past a bound.
    """
    return {
        "overpass_requests": config.overpass_request_bound,
        "brave_requests": (
            config.max_passes
            * config.max_evaluations_per_pass
            * config.evaluation_attempt_limit
        ),
        "census_requests": config.census_request_bound,
    }


def _unit_state(checkpoint: dict[str, Any], unit_key: str) -> dict[str, Any]:
    """Get-or-create durable progression state for one search unit."""
    cell_state = checkpoint.setdefault("cell_state", {})
    if not isinstance(cell_state, dict):
        cell_state = checkpoint["cell_state"] = {}
    state = cell_state.get(unit_key)
    if not isinstance(state, dict):
        state = {"passes": 0, "created": 0, "exhausted": False,
                 "reason": "", "limit": 0}
        cell_state[unit_key] = state
    return state


def _mark_unit_exhausted(
    state: dict[str, Any], *, reason: str, limit: int
) -> None:
    state["exhausted"] = True
    state["reason"] = reason
    state["limit"] = int(limit)


def _unit_runnable(
    cell: RunnerCell,
    state: dict[str, Any],
    *,
    counts: dict[tuple[str, str], int],
    policy: str,
    effective_limit: int,
    max_created_per_unit: int | None = None,
    max_discovery_attempts_per_unit: int | None = None,
) -> bool:
    """Whether a search unit may take another discovery pass.

    The per-unit attempt cap is absolute: no wider window,
    exhaustion reset, or unmet quota reopens a unit whose
    recorded attempts reached it. Exhausted units otherwise stay
    skipped unless a wider window reopens them (a larger pass
    limit sees provider results beyond the exhausted head).
    Under the pilot policy, fulfilled cells are skipped; timed
    reassessment continues past quota by design. Units that
    reached their per-unit creation cap are skipped under every
    policy.
    """
    from app.pilot import TARGET_QUALIFIED_PER_CELL

    if max_discovery_attempts_per_unit is not None and (
        int(state.get("passes", 0)) >= int(max_discovery_attempts_per_unit)
    ):
        return False
    if state.get("exhausted") and int(effective_limit) <= int(state.get("limit", 0)):
        return False
    if policy == "pilot" and (
        counts.get(_canonical_cell_key(cell), 0) >= TARGET_QUALIFIED_PER_CELL
    ):
        return False
    if max_created_per_unit is not None and (
        int(state.get("created", 0)) >= int(max_created_per_unit)
    ):
        return False
    return True


def _record_unit_progress(
    checkpoint: dict[str, Any], unit_key: str, result: Any, effective_limit: int
) -> None:
    """Record one pass against a search unit, exhausting honestly.

    A pass that creates nothing exhausts its exact query: the same
    request deterministically returns the same window, and the
    dedup store only grows, so a repeat could never create more.
    Progress (created > 0) clears exhaustion, including after a
    wider-limit revisit. Window signals are read tolerantly so
    scripted doubles (created-only counts) still exhaust on zeros.
    """
    state = _unit_state(checkpoint, unit_key)
    counts = getattr(result, "counts", None)
    try:
        created = int(getattr(counts, "created", 0) or 0)
    except (TypeError, ValueError):
        created = 0
    state["passes"] = int(state.get("passes", 0)) + 1
    state["created"] = int(state.get("created", 0)) + created
    if created > 0:
        state["exhausted"] = False
        state["reason"] = ""
        return
    raw_returned = getattr(counts, "returned", None)
    try:
        returned = None if raw_returned is None else int(raw_returned)
    except (TypeError, ValueError):
        returned = None
    try:
        unusable = int(getattr(counts, "skipped_unusable", 0) or 0)
    except (TypeError, ValueError):
        unusable = 0
    if returned is None:
        reason = "no created results (unreported window)"
    elif returned == 0:
        reason = "empty window"
    elif unusable == 0:
        reason = "all duplicates at this limit"
    else:
        reason = "no usable results at this limit"
    _mark_unit_exhausted(state, reason=reason, limit=effective_limit)


def _no_runnable_reason(
    config: RunnerConfig,
    checkpoint: dict[str, Any],
    counts: dict[tuple[str, str], int],
    effective_limit: int,
) -> str:
    """Honest completion reason when no search unit may run.

    Mirrors the selection loop (creation-cap skip, then
    :func:`_unit_runnable` branches) so every unit lands in
    exactly one bucket.
    """
    from app.pilot import TARGET_QUALIFIED_PER_CELL

    exhausted = 0
    fulfilled = 0
    capped = 0
    attempts_spent = 0
    for cell in config.cells:
        state = _unit_state(checkpoint, cell.unit_key)
        clamped = int(effective_limit)
        if config.max_created_per_unit is not None:
            clamped = min(
                clamped,
                int(config.max_created_per_unit) - int(state.get("created", 0)),
            )
        if config.max_discovery_attempts_per_unit is not None and (
            int(state.get("passes", 0))
            >= int(config.max_discovery_attempts_per_unit)
        ):
            attempts_spent += 1
        elif config.max_created_per_unit is not None and (
            int(state.get("created", 0)) >= int(config.max_created_per_unit)
        ):
            capped += 1
        elif state.get("exhausted") and clamped <= int(state.get("limit", 0)):
            exhausted += 1
        elif (
            config.policy == "pilot"
            and counts.get(_canonical_cell_key(cell), 0) >= TARGET_QUALIFIED_PER_CELL
        ):
            fulfilled += 1
        else:  # pragma: no cover - unreachable when no unit is runnable
            exhausted += 1
    total = len(config.cells)
    if exhausted == total and fulfilled == 0 and capped == 0 and attempts_spent == 0:
        return "all configured work units exhausted"
    if fulfilled == total and exhausted == 0 and capped == 0 and attempts_spent == 0:
        return "all configured work units fulfilled"
    if capped == total and exhausted == 0 and fulfilled == 0 and attempts_spent == 0:
        return "all configured work units at creation cap"
    if attempts_spent == total and exhausted == 0 and fulfilled == 0 and capped == 0:
        return "all configured work units spent their discovery attempts"
    return (
        f"no runnable discovery units ({exhausted} exhausted, "
        f"{fulfilled} fulfilled, {capped} at creation cap, "
        f"{attempts_spent} attempts spent)"
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
        "evaluation_attempt_limit": config.evaluation_attempt_limit,
        "evaluation_timeout_seconds": config.evaluation_timeout,
        "max_created_per_unit": config.max_created_per_unit,
        "max_discovery_attempts_per_unit": config.max_discovery_attempts_per_unit,
        "max_census_requests": config.max_census_requests,
        "census_attempt_limit": config.census_attempt_limit,
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
    current_units = {cell.unit_key for cell in config.cells}
    for key, value in (saved.get("cell_state") or {}).items():
        if key in current_units and isinstance(value, dict):
            checkpoint["cell_state"][key] = {
                "passes": int(value.get("passes", 0)),
                "created": int(value.get("created", 0)),
                "exhausted": bool(value.get("exhausted", False)),
                "reason": str(value.get("reason", "") or ""),
                "limit": int(value.get("limit", 0)),
            }
    checkpoint["overpass_requests"] = int(saved.get("overpass_requests", 0))
    checkpoint["brave_requests"] = int(saved.get("brave_requests", 0))
    checkpoint["census_requests"] = int(saved.get("census_requests", 0))
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
            "unit": str(pending.get("unit", "") or ""),
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


def _unit_key_for_cell(checkpoint: dict[str, Any], cell_key: str) -> str:
    """Derive the search-unit key for a cell key, when unambiguous.

    Rebuilds unit keys from the checkpoint's own stored cell
    definitions (same county/category/bbox/source shape the
    runner wrote). Returns "" when the cell key matches zero or
    several configured units: callers then restore only totals
    they can attribute exactly, never a guessed unit.
    """
    matches: list[str] = []
    for entry in checkpoint.get("cells") or []:
        if not isinstance(entry, dict):
            continue
        if f"{entry.get('county')}||{entry.get('category')}" != cell_key:
            continue
        bbox = entry.get("bbox") or []
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        try:
            south, west, north, east = (float(value) for value in bbox)
        except (TypeError, ValueError):
            continue
        matches.append(
            f"{entry.get('county')}||{entry.get('category')}||"
            f"{south!r},{west!r},{north!r},{east!r}||{entry.get('source')}"
        )
    return matches[0] if len(matches) == 1 else ""


def _fold_pass_events(
    checkpoint: dict[str, Any],
    run_id: str,
    cell_key: str,
    unit_key: str,
    *,
    events_fn: Callable[[str], list[Any]],
) -> dict[str, Any]:
    """Fold one pass's committed creations plus its accounting.

    Shared by crash recovery and the in-burst failure path: reads
    the exact discovery run's permanent decision events (reads
    only — no provider calls, no property creation), enqueues
    committed creations, and restores the pass's total, per-cell,
    and per-unit creation counts plus its pass sequence and
    request accounting. Only ``created`` outcomes with a
    COMMITTED mutation and a valid property UUID count; skips,
    summaries, and malformed rows are ignored, never adopted.
    Only newly enqueued ids restore creation counts, so a
    re-fold after a crash during recovery cannot double-count.
    An unresolvable unit restores totals but attributes no unit.
    """
    from app.agents.gabriel.service import (
        DECISION_EVENT_TYPE,
        MUTATION_COMMITTED,
    )

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
    backlog = checkpoint.get("backlog") or []
    if not isinstance(backlog, list):
        backlog = []
    evaluations = checkpoint.get("evaluations") or {}
    terminal = set()
    if isinstance(evaluations, dict):
        terminal = {
            str(pid)
            for pid, outcome in evaluations.items()
            if _is_terminal_outcome(outcome)
        }
    settled = {str(pid) for pid in backlog} | terminal
    added = [pid for pid in created_ids if pid not in settled]
    _enqueue_backlog(checkpoint, created_ids, run_id)
    checkpoint["created_total"] = int(checkpoint.get("created_total", 0)) + len(added)
    checkpoint["passes"] = int(checkpoint.get("passes", 0)) + 1
    checkpoint["overpass_requests"] = (
        int(checkpoint.get("overpass_requests", 0)) + 1
    )
    per_cell = checkpoint.get("per_cell")
    if isinstance(per_cell, dict) and cell_key in per_cell:
        per_cell[cell_key] = int(per_cell[cell_key]) + len(added)
    attributed = ""
    if unit_key:
        state = _unit_state(checkpoint, unit_key)
        state["passes"] = int(state.get("passes", 0)) + 1
        state["created"] = int(state.get("created", 0)) + len(added)
        if added:
            state["exhausted"] = False
            state["reason"] = ""
        attributed = unit_key
    return {
        "enqueued": len(created_ids),
        "restored_created": len(added),
        "restored_pass": True,
        "restored_request": True,
        "unit_key": attributed,
    }


def _recover_pending_pass(
    checkpoint: dict[str, Any],
    *,
    events_fn: Callable[[str], list[Any]] | None = None,
) -> dict[str, Any]:
    """Fold an interrupted pass's committed candidates into the backlog.

    Reads the exact discovery run's permanent decision events
    (database reads only — no provider calls, no property creation)
    and enqueues committed creations, restoring the pass's total,
    per-cell, and per-unit creation counts plus its pass sequence
    and request accounting. Only ``created`` outcomes with
    a COMMITTED mutation and a valid property UUID count; skips,
    summaries, and malformed rows are ignored, never adopted.
    Raises DatabaseUnavailableError when the event store cannot be
    read (the caller must abort rather than proceed blind).
    """
    pending = checkpoint.get("pending_pass") or {}
    run_id = str(pending.get("discovery_run_id", "") or "")
    folded = checkpoint.get("discovery_runs") or []
    if not run_id or run_id in folded:
        checkpoint["pending_pass"] = None
        return {"discovery_run_id": run_id, "enqueued": 0, "already_folded": True}
    if events_fn is None:
        from app.observability.store import by_run as events_fn
    cell_key = str(pending.get("cell", "") or "")
    unit_key = str(pending.get("unit", "") or "")
    if not unit_key:
        unit_key = _unit_key_for_cell(checkpoint, cell_key)
    folded_note = _fold_pass_events(
        checkpoint, run_id, cell_key, unit_key, events_fn=events_fn
    )
    checkpoint["pending_pass"] = None
    return {
        "discovery_run_id": run_id,
        "already_folded": False,
        **folded_note,
    }


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


def _brave_used_for(assignment_id: str, query: str) -> int:
    """Brave attempts charged to one evaluation run (0 when absent).

    Reads the run's durable usage counter, which increments before
    each transmission — ambiguous and failed transmitted attempts
    count, replays and refusals do not. Zero when the database is
    unconfigured (scripted doubles) or the run does not exist yet;
    real outages propagate so the burst aborts loudly.
    """
    from app.db import DatabaseNotConfiguredError
    from app.mvp import evaluation_key
    from app.properties_db import get_connection
    from app.workflow_db import get_run_by_key_in_transaction

    try:
        conn = get_connection()
    except DatabaseNotConfiguredError:
        return 0
    try:
        run = get_run_by_key_in_transaction(conn, evaluation_key(assignment_id, query))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if run is None:
        return 0
    try:
        return max(0, int((run.usage or {}).get("brave_attempts", 0)))
    except (TypeError, ValueError):
        return 0


def _census_used_for(assignment_id: str, query: str) -> int:
    """Census attempts charged to one evaluation run (0 when absent).

    Reads the run's durable usage counter, which the claim
    commit reserves before transmission — replays, resumes, and
    pre-claim refusals add nothing, and no crash can undercount
    or double-count it. Zero when the database is unconfigured
    (scripted doubles) or the run does not exist yet; real
    outages propagate so the burst aborts loudly.
    """
    from app.db import DatabaseNotConfiguredError
    from app.mvp import evaluation_key
    from app.properties_db import get_connection
    from app.workflow_db import get_run_by_key_in_transaction

    try:
        conn = get_connection()
    except DatabaseNotConfiguredError:
        return 0
    try:
        run = get_run_by_key_in_transaction(conn, evaluation_key(assignment_id, query))
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if run is None:
        return 0
    try:
        return max(0, int((run.usage or {}).get("census_attempts", 0)))
    except (TypeError, ValueError):
        return 0


def _census_allowed_for_eval(config: RunnerConfig, burst_spent: int) -> bool:
    """Whether one more evaluation may attempt Census admission.

    Pure gate over the configured envelope: Census transmits only
    with an explicit burst budget that still has room and a
    nonzero per-property limit. Past the budget, evaluations run
    without county admission rather than transmitting.
    """
    return (
        config.census_request_bound > 0
        and int(burst_spent) < config.census_request_bound
        and config.census_attempt_limit > 0
    )


def evaluate_property(
    scope: str,
    property_id: str,
    *,
    approved: bool,
    timeout_seconds: float,
    brave_attempt_limit: int = 9,
    census_attempt_limit: int = 0,
    census_principal: str | None = None,
) -> dict[str, Any]:
    """Drive one discovered property through the existing coordinator.

    Uses the property's own operational identity end to end (one
    pipeline): a deterministic assignment derived from that identity,
    acquisition through the coordinator's budget/ambiguity guards,
    dossier persistence, qualification, and Raphael review.
    ``brave_attempt_limit`` bounds Brave attempts for a newly created
    run (a resumed run keeps its persisted limit).
    ``census_attempt_limit`` (0..1, default disabled) allows at most
    one Census county-admission request for the property; enabling
    it requires ``census_principal`` attribution. Outcomes:

    - ``evaluated``: coordinator summary with decision + qualified count.
    - ``blocked``: coordinator refused (ambiguous prior attempt or
      similar no-retry signal); terminal, never retried blindly.
    - ``skipped``: property-specific dead end (missing row, no usable
      address, invalid identity); terminal.
    - ``error``: anything else went wrong; re-driven on resume.

    Every post-coordinator outcome carries ``brave_attempts`` and
    ``census_attempts``: the durable usage deltas for this call.
    Brave counts transmitted attempts; Census counts claimed
    attempts (reserved at claim time, before transmission).
    Replays and refusals add nothing to either.

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
    assignment_id = f"runner:{scope}:{pid}"
    brave_before = _brave_used_for(assignment_id, query)
    census_before = _census_used_for(assignment_id, query)
    try:
        summary = start_evaluation_audited(
            assignment_id, query,
            approved=True, timeout_seconds=timeout_seconds, property_id=pid,
            brave_attempt_limit=brave_attempt_limit,
            census_attempt_limit=census_attempt_limit,
            census_principal=census_principal,
        )
    except WorkflowError as exc:
        spent = max(0, _brave_used_for(assignment_id, query) - brave_before)
        census_spent = max(0, _census_used_for(assignment_id, query) - census_before)
        message = str(exc)
        if any(marker in message for marker in _NO_RETRY_MARKERS):
            return {
                "outcome": "blocked", "reason": message,
                "brave_attempts": spent, "census_attempts": census_spent,
            }
        return {
            "outcome": "error", "reason": message,
            "brave_attempts": spent, "census_attempts": census_spent,
        }
    spent = max(0, _brave_used_for(assignment_id, query) - brave_before)
    census_spent = max(0, _census_used_for(assignment_id, query) - census_before)
    qualification = summary.get("qualification") or {}
    dossiers = len(qualification) if isinstance(qualification, dict) else 0
    qualified = (
        sum(1 for value in qualification.values() if value == "qualified")
        if isinstance(qualification, dict)
        else 0
    )
    census_block = summary.get("census") or {}
    return {
        "outcome": "evaluated",
        "run_id": str(summary.get("run_id", "")),
        "decision": str(summary.get("decision", "")),
        "dossiers": dossiers,
        "qualified": qualified,
        "brave_attempts": spent,
        "census_attempts": census_spent,
        "census": str(census_block.get("outcome", "") or "skipped"),
    }


def _coordinator_enabled() -> bool:
    from app.mvp import workflow_enabled

    return workflow_enabled()


def _drive_evaluations(
    evaluate_fn: Callable[[str, str], dict[str, Any]],
    config: RunnerConfig,
    checkpoint: dict[str, Any],
    report: dict[str, Any],
    *,
    preempt_fn: Callable[[], str | None] | None = None,
) -> tuple[int, int] | None:
    """Drive the durable backlog head through the coordinator (one pass).

    The backlog (persisted before caps apply) is the only work
    source: resume never depends on a candidate reappearing in a
    later pass's ``created_ids``. Terminal outcomes leave the
    backlog; ``error`` outcomes stay queued for resume. A crashed
    in-flight evaluation resumes under its deterministic assignment,
    where the coordinator either replays the completed artifact or
    refuses an ambiguous attempt — never blindly re-running a
    possibly-charged call. ``preempt_fn`` is consulted between
    evaluations; a non-empty return stops the drive with that
    reason recorded. Brave and Census spend accrue monotonically
    per outcome (retried pids re-add, never overwrite past
    spend). Returns (evaluated, qualified) or None on database
    outage (backlog intact for the finish-path re-save).
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
        if preempt_fn is not None:
            try:
                preempted = preempt_fn()
            except (DatabaseUnavailableError, DatabaseNotConfiguredError):
                return None
            if preempted:
                report["evaluations_preempted"] = preempted
                break
        try:
            outcome = evaluate_fn(config.scope, str(pid))
        except (DatabaseUnavailableError, DatabaseNotConfiguredError):
            return None
        if not isinstance(outcome, dict) or not outcome.get("outcome"):
            outcome = {"outcome": "error", "reason": "empty evaluation outcome"}
        evaluations[str(pid)] = outcome
        driven += 1
        try:
            spent = int(outcome.get("brave_attempts", 0) or 0)
        except (TypeError, ValueError):
            spent = 0
        checkpoint["brave_requests"] = int(checkpoint.get("brave_requests", 0)) + max(0, spent)
        try:
            census_spent = int(outcome.get("census_attempts", 0) or 0)
        except (TypeError, ValueError):
            census_spent = 0
        checkpoint["census_requests"] = (
            int(checkpoint.get("census_requests", 0)) + max(0, census_spent)
        )
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
    events_fn: Callable[[str], list[Any]] | None = None,
) -> dict[str, Any]:
    """Execute one bounded worker burst, checkpointing every pass.

    ``get_conn`` supplies short-lived connections the worker commits
    and closes itself. ``discovery_fn`` defaults to the real
    ``run_discovery`` and is always called as
    ``discovery_fn(cell, run_id, limit)`` with the pass's effective
    creation-clamped limit; tests inject scripted doubles. Returns a
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
    A discovery call that raises folds its partial committed
    creations through the same accounting (see
    :func:`_fold_pass_events`) and consumes its sequence, so the
    next pass never reuses the failed run id. Discovery limits
    (creation ceiling, pilot quota) stop new discovery only: a
    pending backlog keeps draining in bounded rounds under the
    same duration/stop/evaluation limits. Reported ``created``
    counts property records; only coordinator ``qualified``
    outcomes count toward dossier qualification — the two are
    never substituted.
    """
    config.validate()
    sleep = sleep_fn or time.sleep
    clock = clock_fn or time.monotonic
    if events_fn is None:
        from app.observability.store import by_run as _default_events_fn

        _pass_events_fn = _default_events_fn
    else:
        _pass_events_fn = events_fn
    bounds = burst_request_bounds(config)
    if discovery_fn is None:
        from app.agents.gabriel.schemas import BoundingBoxInput, DiscoveryRunRequest
        from app.agents.gabriel.service import run_discovery
        from app.agents.gabriel.sources.base import UnknownSourceError, get_source

        for cell in config.cells:
            try:
                get_source(cell.source)
            except UnknownSourceError as exc:
                raise RunnerError(f"unknown discovery source: {cell.source!r}") from exc
            # Fail fast on deterministic request-shape errors before any
            # provider request is attempted or counted.
            south, west, north, east = cell.bbox
            DiscoveryRunRequest(
                source=cell.source,
                bbox=BoundingBoxInput(
                    south=south, west=west, north=north, east=east
                ),
                categories=[cell.category],
                limit=config.pass_limit,
                timeout_seconds=config.pass_timeout_seconds,
            )

        def _real(cell: RunnerCell, run_id: str, limit: int) -> Any:
            south, west, north, east = cell.bbox
            return run_discovery(
                DiscoveryRunRequest(
                    source=cell.source,
                    bbox=BoundingBoxInput(
                        south=south, west=west, north=north, east=east
                    ),
                    categories=[cell.category],
                    limit=int(limit),
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
            timeout = config.evaluation_timeout
            attempt_limit = config.evaluation_attempt_limit

            def _default_evaluate(scope: str, pid: str) -> dict[str, Any]:
                burst_spent = int(checkpoint.get("census_requests", 0)) - census_baseline
                if _census_allowed_for_eval(config, burst_spent):
                    census_limit = config.census_attempt_limit
                else:
                    census_limit = 0
                    if config.census_request_bound > 0:
                        report["census_preempted"] = "census request bound reached"
                return evaluate_property(
                    scope, pid, approved=approved,
                    timeout_seconds=timeout,
                    brave_attempt_limit=attempt_limit,
                    census_attempt_limit=census_limit,
                    census_principal=f"runner:{scope}",
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
        "request_bounds": dict(bounds),
        "overpass_requests": 0,
        "brave_requests": 0,
        "census_requests": 0,
        "census_preempted": None,
        "cells": [],
        "credited_new": 0,
        "remaining_quota": None,
        "raphael_decisions": {},
        "exhausted_units": [],
        "unresolved": {"queued_backlog": 0, "blocked": 0, "errors": 0},
        "evaluations_precondition": evaluations_precondition,
        "evaluations_preempted": None,
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
    overpass_baseline = int(checkpoint.get("overpass_requests", 0))
    brave_baseline = int(checkpoint.get("brave_requests", 0))
    census_baseline = int(checkpoint.get("census_requests", 0))
    counts_before: dict[tuple[str, str], int] | None = None
    counts_last: dict[tuple[str, str], int] | None = None
    evaluations_halted: str | None = None
    burst_decisions: dict[str, int] = {}
    if evaluate_fn is not None:
        _inner_evaluate = evaluate_fn

        def _counting_evaluate(scope: str, pid: str) -> dict[str, Any]:
            outcome = _inner_evaluate(scope, pid)
            if isinstance(outcome, dict) and outcome.get("outcome") == "evaluated":
                decision = str(outcome.get("decision", "") or "") or "unknown"
                burst_decisions[decision] = burst_decisions.get(decision, 0) + 1
            return outcome

        evaluate_fn = _counting_evaluate

    def _usage() -> dict[str, Any]:
        return {
            "runner_version": RUNNER_VERSION,
            "passes": burst_passes,
            "created": burst_created,
            "evaluated": burst_evaluated,
            "qualified_dossiers": burst_qualified,
            "overpass_requests": (
                int(checkpoint.get("overpass_requests", 0)) - overpass_baseline
            ),
            "brave_requests": (
                int(checkpoint.get("brave_requests", 0)) - brave_baseline
            ),
            "census_requests": (
                int(checkpoint.get("census_requests", 0)) - census_baseline
            ),
        }

    def _refresh_final_counts() -> None:
        """Fresh canonical counts for the final report, or unavailable.

        The last evaluation round may earn credit after the loop's
        most recent quota read (notably when the pass budget fills
        immediately after that round), so the final report always
        re-reads from the same canonical source. A failed read
        marks quota progress unavailable instead of presenting
        stale counts as current; committed work and recovery are
        unaffected.
        """
        nonlocal counts_before, counts_last
        try:
            fresh = dict(counts_fn())
        except Exception:
            counts_last = None
            return
        if counts_before is None:
            counts_before = dict(fresh)
        counts_last = dict(fresh)

    def _tally_cells() -> None:
        from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER
        from app.pilot import PILOT_CATEGORIES, TARGET_QUALIFIED_PER_CELL

        if counts_last is None:
            # Quota progress unreadable: report it unavailable with
            # no per-cell rows, never zeros or stale counts.
            report["cells"] = []
            report["remaining_quota"] = None
            report["credited_new"] = None
            return
        rows: list[dict[str, Any]] = []
        remaining = 0
        credited = 0
        for county in COUNTY_PRIORITY_ORDER:
            for category in PILOT_CATEGORIES:
                key = (county, category)
                have = int(counts_last.get(key, 0))
                deficit = max(0, TARGET_QUALIFIED_PER_CELL - have)
                remaining += deficit
                if counts_before is not None:
                    credited += max(0, have - int(counts_before.get(key, 0)))
                rows.append({
                    "county": county, "category": category,
                    "credited": have, "target": TARGET_QUALIFIED_PER_CELL,
                    "deficit": deficit,
                })
        report["cells"] = rows
        report["remaining_quota"] = remaining
        report["credited_new"] = credited

    def _tally_outcomes() -> None:
        blocked = 0
        errors = 0
        for outcome in (checkpoint.get("evaluations") or {}).values():
            if not isinstance(outcome, dict):
                continue
            kind = str(outcome.get("outcome", "") or "")
            if kind == "blocked":
                blocked += 1
            elif kind == "error":
                errors += 1
        report["raphael_decisions"] = dict(burst_decisions)
        report["unresolved"] = {
            "queued_backlog": _backlog_depth(checkpoint),
            "blocked": blocked,
            "errors": errors,
        }
        report["exhausted_units"] = sorted(
            key
            for key, state in (checkpoint.get("cell_state") or {}).items()
            if isinstance(state, dict) and state.get("exhausted")
        )
        report["units"] = [
            {
                "unit_key": cell.unit_key,
                "cell": cell.key,
                "county": cell.county,
                "category": cell.category,
                "attempts": int(state.get("passes", 0)),
                "created": int(state.get("created", 0)),
                "exhausted": bool(state.get("exhausted", False)),
                "reason": str(state.get("reason", "") or ""),
            }
            for cell in config.cells
            for state in [
                (checkpoint.get("cell_state") or {}).get(cell.unit_key) or {}
            ]
        ]

    def _tally() -> None:
        report["passes"] = burst_passes
        report["created"] = burst_created
        report["evaluated"] = burst_evaluated
        report["qualified_dossiers"] = burst_qualified
        report["overpass_requests"] = (
            int(checkpoint.get("overpass_requests", 0)) - overpass_baseline
        )
        report["brave_requests"] = (
            int(checkpoint.get("brave_requests", 0)) - brave_baseline
        )
        report["census_requests"] = (
            int(checkpoint.get("census_requests", 0)) - census_baseline
        )
        _tally_cells()
        _tally_outcomes()

    def _should_preempt() -> str | None:
        """Stop reason between evaluations, or None to continue."""
        if _sabbath_blackout(now_fn() if now_fn else None):
            return "Sabbath blackout window"
        if clock() - started >= config.max_duration_seconds:
            return "duration limit reached"
        preempt_conn = _connect()
        try:
            preempt_state = run_state_in_transaction(preempt_conn, run_id)
        finally:
            _close(preempt_conn)
        if preempt_state is not None and preempt_state["stop_requested"]:
            return "operator stop observed"
        spent = int(checkpoint.get("brave_requests", 0)) - brave_baseline
        if spent >= bounds["brave_requests"]:
            return "brave request bound reached"
        return None

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
        _refresh_final_counts()
        _tally()
        return report

    def _drain_or_finish(reason: str) -> bool:
        """One evaluation-only round; False when the burst finished.

        Shared by every discovery-resting path (creation ceiling,
        quota satisfied, request budget spent, no runnable units):
        drain the durable backlog within the pass budget, honoring
        preemption between evaluations, or finish completed when no
        drive is possible.
        """
        nonlocal burst_evaluated, burst_qualified, burst_passes, evaluations_halted
        if (
            evaluate_fn is None
            or evaluations_halted is not None
            or _backlog_depth(checkpoint) == 0
        ):
            _finish("completed", reason)
            return False
        driven = _drive_evaluations(
            evaluate_fn, config, checkpoint, report, preempt_fn=_should_preempt
        )
        if driven is None:  # database outage inside the coordinator
            _finish("aborted", "evaluation database unavailable")
            return False
        burst_evaluated += driven[0]
        burst_qualified += driven[1]
        burst_passes += 1
        if not _persist_checkpoint():
            return False
        preempted = report.get("evaluations_preempted")
        if preempted and preempted != "brave request bound reached":
            _finish("paused", preempted)
            return False
        if preempted:
            evaluations_halted = preempted
        sleep(config.pacing_seconds)
        return True

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
            recovered = _recover_pending_pass(
                checkpoint, events_fn=_pass_events_fn
            )
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
        if counts_before is None:
            counts_before = dict(counts)
        counts_last = dict(counts)
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
            if not _drain_or_finish(reason):
                return report
            continue
        spent_overpass = int(checkpoint.get("overpass_requests", 0)) - overpass_baseline
        if spent_overpass >= bounds["overpass_requests"]:
            if not _drain_or_finish("overpass request budget exhausted"):
                return report
            continue
        ordered = (
            _cell_order_pilot(config.cells, counts)
            if config.policy == "pilot"
            else _cell_order_timed(config.cells, checkpoint)
        )
        # A pass can never exceed the remaining creation allowance:
        # the effective limit is clamped before discovery runs.
        remaining = config.max_created_total - int(checkpoint["created_total"])
        effective_limit = min(config.pass_limit, max(remaining, 1))
        cell = None
        selected_effective = effective_limit
        for step in range(len(ordered)):
            candidate = ordered[(int(checkpoint["cursor"]) + step) % len(ordered)]
            state = _unit_state(checkpoint, candidate.unit_key)
            if config.max_created_per_unit is not None:
                unit_remaining = int(config.max_created_per_unit) - int(
                    state.get("created", 0)
                )
                if unit_remaining <= 0:
                    continue
                unit_effective = min(effective_limit, unit_remaining)
            else:
                unit_effective = effective_limit
            if _unit_runnable(
                candidate, state, counts=counts,
                policy=config.policy, effective_limit=unit_effective,
                max_created_per_unit=config.max_created_per_unit,
                max_discovery_attempts_per_unit=(
                    config.max_discovery_attempts_per_unit
                ),
            ):
                cell = candidate
                selected_effective = unit_effective
                checkpoint["cursor"] = int(checkpoint["cursor"]) + step + 1
                break
        if cell is None:
            reason = _no_runnable_reason(config, checkpoint, counts, effective_limit)
            if not _drain_or_finish(reason):
                return report
            continue
        pending_id = _pass_run_id(config.scope, run_id, checkpoint["passes"])
        checkpoint["pending_pass"] = {
            "seq": checkpoint["passes"],
            "discovery_run_id": pending_id,
            "cell": cell.key,
            "unit": cell.unit_key,
        }
        if not _persist_checkpoint():
            return report
        try:
            result = discovery_fn(cell, pending_id, selected_effective)
            created = int(getattr(result.counts, "created", 0))
        except Exception as exc:
            # Attempts count even when they fail: a transmitted
            # request was spent. Pure pre-transmit validation
            # failures are fail-fast at burst start, so anything
            # reaching here conservatively counts as one request.
            # Partial committed creations fold exactly like crash
            # recovery (same helper, same accounting): the attempt
            # records against the unit and the sequence is consumed
            # so the next pass never reuses this run id.
            try:
                _fold_pass_events(
                    checkpoint, pending_id, cell.key, cell.unit_key,
                    events_fn=_pass_events_fn,
                )
            except DatabaseNotConfiguredError:
                # No database: nothing could have committed, but
                # the attempt itself still counts.
                checkpoint["overpass_requests"] = (
                    int(checkpoint.get("overpass_requests", 0)) + 1
                )
                checkpoint["passes"] = int(checkpoint.get("passes", 0)) + 1
                failed_state = _unit_state(checkpoint, cell.unit_key)
                failed_state["passes"] = int(failed_state.get("passes", 0)) + 1
            except DatabaseUnavailableError:
                return _finish("aborted", "failed-pass event fold unavailable")
            checkpoint["pending_pass"] = None
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
        checkpoint["overpass_requests"] = int(checkpoint.get("overpass_requests", 0)) + 1
        burst_passes += 1
        burst_created += created
        checkpoint["passes"] = int(checkpoint["passes"]) + 1
        checkpoint["created_total"] = int(checkpoint["created_total"]) + created
        checkpoint["per_cell"][cell.key] = int(checkpoint["per_cell"][cell.key]) + created
        _record_unit_progress(checkpoint, cell.unit_key, result, selected_effective)
        try:
            pass_ids = [str(i) for i in (getattr(result, "created_ids", None) or [])]
        except TypeError:
            pass_ids = []
        _enqueue_backlog(checkpoint, pass_ids, getattr(result, "run_id", "") or "")
        checkpoint["pending_pass"] = None
        if not _persist_checkpoint():
            return report
        if evaluate_fn is not None and evaluations_halted is None:
            driven = _drive_evaluations(
                evaluate_fn, config, checkpoint, report, preempt_fn=_should_preempt
            )
            if driven is None:  # database outage inside the coordinator
                return _finish("aborted", "evaluation database unavailable")
            burst_evaluated += driven[0]
            burst_qualified += driven[1]
            preempted = report.get("evaluations_preempted")
            if preempted and preempted != "brave request bound reached":
                return _finish("paused", preempted)
            if preempted:
                evaluations_halted = preempted
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


_ENVELOPE_KEYS = frozenset({
    "envelope",
    "description",
    "policy",
    "units",
    "max_passes",
    "max_duration_seconds",
    "max_created_total",
    "max_created_per_unit",
    "max_discovery_attempts_per_unit",
    "pass_limit",
    "pass_timeout_seconds",
    "max_evaluations_per_pass",
    "evaluation_attempt_limit",
    "evaluation_timeout_seconds",
    "max_overpass_requests",
    "max_census_requests",
    "census_attempt_limit",
    "max_consecutive_failures",
    "pacing_seconds",
    "backoff_cap_seconds",
    "claim_lease_seconds",
    "meta_calls",
    "outreach",
})


def envelope_config_from_json(
    path: str,
    *,
    scope: str,
    resume: bool = False,
    created_by: str = "envelope",
    acquire_approved: bool = False,
) -> RunnerConfig:
    """Build one complete burst config from an envelope file (strict).

    The envelope pins every bound preview and execution share;
    only operational identity (scope/created-by), resume, and the
    acquisition grant ride the command line. Unknown keys,
    missing units, non-numeric bounds, nonzero Meta calls, or
    anything but ``"disabled"`` outreach refuse loudly: preview
    and execution load through this same function, so the shown
    envelope is the enforced envelope.
    """
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise RunnerError("envelope file must hold one JSON object")
    for key in raw:
        if key not in _ENVELOPE_KEYS:
            raise RunnerError(f"unknown envelope key: {key!r}")
    units = raw.get("units")
    if not isinstance(units, list) or not units:
        raise RunnerError("envelope units must be a non-empty list")
    cells = []
    for item in units:
        if not isinstance(item, dict):
            raise RunnerError("each envelope unit must be an object")
        bbox = item.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in bbox
            )
        ):
            raise RunnerError("each envelope unit needs bbox [south, west, north, east]")
        cells.append(
            RunnerCell(
                county=str(item.get("county", "")),
                category=str(item.get("category", "")),
                bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                source=str(item.get("source", "overpass") or "overpass"),
            )
        )

    def _int(name: str, default: int | None) -> int | None:
        value = raw.get(name, default)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RunnerError(f"envelope {name} must be numeric")
        if float(value) != int(value):
            raise RunnerError(f"envelope {name} must be an integer")
        return int(value)

    def _float(name: str, default: float | None) -> float | None:
        value = raw.get(name, default)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RunnerError(f"envelope {name} must be numeric")
        return float(value)

    if raw.get("meta_calls", 0) != 0:
        raise RunnerError("envelope meta_calls must be 0 (no Meta execution)")
    if raw.get("outreach", "disabled") != "disabled":
        raise RunnerError('envelope outreach must be "disabled"')

    def _req(value: int | float | None, default: int | float) -> Any:
        # Explicit null falls back to the default; explicit zero
        # passes through to RunnerConfig.validate, which accepts
        # or rejects it per field (never silently replaced).
        return default if value is None else value

    config = RunnerConfig(
        scope=scope,
        policy=str(raw.get("policy", "pilot") or "pilot"),
        cells=cells,
        max_passes=_req(_int("max_passes", 1), 1),
        max_duration_seconds=_req(_float("max_duration_seconds", 600.0), 600.0
        ),
        max_created_total=_req(_int("max_created_total", 50), 50
        ),
        pass_limit=_req(_int("pass_limit", 10), 10),
        pass_timeout_seconds=_req(_float("pass_timeout_seconds", 15.0), 15.0
        ),
        pacing_seconds=_req(_float("pacing_seconds", DEFAULT_PACING_SECONDS),
            DEFAULT_PACING_SECONDS,
        ),
        backoff_cap_seconds=_req(_float("backoff_cap_seconds", DEFAULT_BACKOFF_CAP_SECONDS),
            DEFAULT_BACKOFF_CAP_SECONDS,
        ),
        max_consecutive_failures=_req(_int("max_consecutive_failures", 3), 3
        ),
        claim_lease_seconds=_req(_float("claim_lease_seconds", DEFAULT_CLAIM_LEASE_SECONDS),
            DEFAULT_CLAIM_LEASE_SECONDS,
        ),
        resume=resume,
        created_by=created_by,
        acquire_approved=acquire_approved,
        max_evaluations_per_pass=_req(_int("max_evaluations_per_pass", 5), 5
        ),
        evaluation_attempt_limit=_req(_int("evaluation_attempt_limit", 9), 9
        ),
        evaluation_timeout_seconds=_float("evaluation_timeout_seconds", None),
        max_overpass_requests=_int("max_overpass_requests", None),
        max_created_per_unit=_int("max_created_per_unit", None),
        max_discovery_attempts_per_unit=_int(
            "max_discovery_attempts_per_unit", None
        ),
        max_census_requests=_int("max_census_requests", None),
        census_attempt_limit=_req(_int("census_attempt_limit", 1), 1
        ),
    )
    config.validate()
    return config


def main(argv: list[str] | None = None) -> int:
    """CLI: run|status|stop|preview. ``run`` executes one bounded burst only."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("run", "status", "stop", "preview"):
        print(
            "usage: python -m app.discovery_runner run|status|stop|preview "
            "--scope SCOPE [--policy pilot|timed] [--cells FILE] "
            "[--max-passes N] [--max-duration SECONDS] [--max-created N] "
            "[--resume] [--created-by LABEL] [--acquire-approved] "
            "[--max-evaluations N] [--evaluation-attempt-limit 1..18] "
            "[--evaluation-timeout SECONDS] [--max-overpass-requests N] "
            "[--max-created-per-unit N] [--max-discovery-attempts-per-unit N] "
            "[--max-census-requests N] [--envelope FILE] "
            "[--census-attempt-limit 0..1]",
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
        envelope_path = _opt("--envelope") or ""
        if envelope_path:
            for flag in (
                "--cells", "--policy", "--max-passes", "--max-duration",
                "--max-created", "--max-evaluations",
                "--evaluation-attempt-limit", "--evaluation-timeout",
                "--max-overpass-requests", "--max-created-per-unit",
                "--max-discovery-attempts-per-unit", "--max-census-requests",
                "--census-attempt-limit",
            ):
                if flag in opts:
                    raise RunnerError(
                        f"{flag} cannot be combined with --envelope "
                        "(the envelope pins every bound)"
                    )
            if not scope:
                raise RunnerError(
                    f"--scope and --envelope are required for {command}"
                )
            config = envelope_config_from_json(
                envelope_path,
                scope=scope,
                resume="--resume" in opts,
                created_by=_opt("--created-by", "cli") or "cli",
                acquire_approved="--acquire-approved" in opts,
            )
        else:
            if not scope or not cells_path:
                raise RunnerError(
                    f"--scope and --cells are required for {command}"
                )
            raw_eval_timeout = _opt("--evaluation-timeout")
            raw_overpass = _opt("--max-overpass-requests")
            raw_per_unit = _opt("--max-created-per-unit")
            raw_attempts = _opt("--max-discovery-attempts-per-unit")
            raw_census = _opt("--max-census-requests")
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
                evaluation_attempt_limit=int(_opt("--evaluation-attempt-limit", "9") or "9"),
                evaluation_timeout_seconds=(
                    float(raw_eval_timeout) if raw_eval_timeout is not None else None
                ),
                max_overpass_requests=(
                    int(raw_overpass) if raw_overpass is not None else None
                ),
                max_created_per_unit=(
                    int(raw_per_unit) if raw_per_unit is not None else None
                ),
                max_discovery_attempts_per_unit=(
                    int(raw_attempts) if raw_attempts is not None else None
                ),
                max_census_requests=(
                    int(raw_census) if raw_census is not None else None
                ),
                census_attempt_limit=int(_opt("--census-attempt-limit", "1") or "1"),
            )
            config.validate()
        if command == "preview":
            print(json.dumps({
                "scope": config.scope,
                "policy": config.policy,
                "units": [
                    {
                        "county": cell.county,
                        "category": cell.category,
                        "bbox": list(cell.bbox),
                        "source": cell.source,
                    }
                    for cell in config.cells
                ],
                "request_bounds": burst_request_bounds(config),
                "max_created_total": config.max_created_total,
                "max_created_per_unit": config.max_created_per_unit,
                "max_discovery_attempts_per_unit": (
                    config.max_discovery_attempts_per_unit
                ),
                "max_evaluations_per_pass": config.max_evaluations_per_pass,
                "evaluation_attempt_limit": config.evaluation_attempt_limit,
                "census_attempt_limit": config.census_attempt_limit,
                "pass_timeout_seconds": config.pass_timeout_seconds,
                "evaluation_timeout_seconds": config.evaluation_timeout,
                "meta_calls": 0,
                "outreach": "disabled",
                "acquire_approved": config.acquire_approved,
                "max_passes": config.max_passes,
                "max_duration_seconds": config.max_duration_seconds,
            }, default=str))
            return 0
        report = run_worker(_connect, config)
        print(json.dumps(report, default=str))
        return 0 if report.get("outcome") in ("completed", "paused", "busy") else 1
    except (RunnerError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
