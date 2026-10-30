"""Big Game Hunter target-balancing scheduler (B5-2A.2b-0a).

Pure, deterministic research-assignment scheduling: given a set of eligible
candidates and per-cell outstanding dossier needs, select at most
``BATCH_SIZE`` distinct assignments honoring hunting priority, group
maxima/minima, and county/category fairness.

This module performs no I/O, starts no timers, and runs nothing in the
background. It is invoked only as a pure planning function (or from an
explicitly authorized operator workflow); it does not activate autonomous
discovery.

Scheduling vocabulary (never conflated):

- Hunting priority: research scheduling order (this module).
- Opportunity score: property attractiveness (ranking.py, untouched).
- Qualification: whether a dossier meets evidence requirements (elsewhere).

A scheduled research assignment is NOT a completed qualified dossier, and an
unverified county assignment never counts toward a cell quota.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER

BATCH_SIZE = 144
GROUP_MINIMUM = 36
GROUP_MAXIMUM = 36

HIGH_RISE = "high_rise"
COMMERCIAL = "commercial_property"
WAREHOUSE = "warehouse_industrial"
CANONICAL_CATEGORIES = frozenset({HIGH_RISE, COMMERCIAL, WAREHOUSE})

_KNOWN_COUNTIES = frozenset(COUNTY_PRIORITY_ORDER)


@dataclass(frozen=True)
class SchedulableCandidate:
    """One eligible research opportunity (identity already established)."""

    candidate_id: str
    category: str  # one of the three canonical categories
    is_downtown: bool = False  # verified downtown only; never inferred
    county: str | None = None  # verified county (canonical or display form), else None
    economic_signal: float | None = None  # tier-3 tiebreak; None sorts last


@dataclass(frozen=True)
class GroupOutcome:
    """Whether a protected minimum was met, and why not if missed."""

    scheduled: int
    eligible: int
    target: int
    met: bool
    reason: str  # "met" | "insufficient_work" | "insufficient_capacity" | "scheduling_defect"


@dataclass(frozen=True)
class BatchSchedule:
    """One bounded batch: scheduled ids in priority order plus accounting."""

    scheduled: tuple[str, ...]
    preserved: tuple[str, ...]  # eligible but unscheduled; retained for later batches
    high_rise: int  # distinct high-rise assignments (max 36)
    downtown_commercial: int  # distinct verified-downtown commercial assignments (max 36)
    commercial_anywhere: int  # distinct commercial assignments incl. downtown (min 36)
    warehouse: int  # distinct warehouse/industrial assignments (min 36)
    commercial_outcome: GroupOutcome
    warehouse_outcome: GroupOutcome
    cell_assignments: dict[tuple[str, str], int] = field(default_factory=dict)
    unverified_count: int = 0  # scheduled assignments without a verified county


def _normalize_county(county: str | None) -> str | None:
    """Canonical county key, or None when unverified/unrecognized.

    Unknown county spellings join the unverified bucket rather than
    failing: county uncertainty must never exclude a candidate (§2.5.7).
    """
    if county is None:
        return None
    key = county.strip().lower()
    return key if key in _KNOWN_COUNTIES else None


def _normalize_cell_need(
    cell_need: Mapping[tuple[str, str], int],
) -> dict[tuple[str, str], int]:
    """Validate operator-supplied progress state (fail closed on garbage)."""
    normalized: dict[tuple[str, str], int] = {}
    for (county, category), need in cell_need.items():
        key = county.strip().lower()
        if key not in _KNOWN_COUNTIES:
            raise ValueError(f"unknown county in cell progress: {county!r}")
        if category not in CANONICAL_CATEGORIES:
            raise ValueError(f"unknown category in cell progress: {category!r}")
        if need < 0:
            raise ValueError(f"negative outstanding need for {(county, category)!r}")
        normalized[(key, category)] = need
    return normalized


def _dedupe(candidates: list[SchedulableCandidate]) -> list[SchedulableCandidate]:
    """One record per candidate id; conflicts resolve deterministically."""
    by_id: dict[str, SchedulableCandidate] = {}
    for candidate in candidates:
        if candidate.category not in CANONICAL_CATEGORIES:
            raise ValueError(f"unknown candidate category: {candidate.category!r}")
        seen = by_id.get(candidate.candidate_id)
        if seen is None:
            by_id[candidate.candidate_id] = candidate
            continue
        # Same id seen twice: keep the smallest full record so input
        # order cannot change the outcome.
        key = lambda c: (
            c.category,
            c.county or "",
            c.is_downtown,
            c.economic_signal is None,
            c.economic_signal or 0.0,
        )
        by_id[candidate.candidate_id] = min(seen, candidate, key=key)
    return list(by_id.values())


def _need_of(
    candidate: SchedulableCandidate,
    county_key: str | None,
    cell_need: Mapping[tuple[str, str], int],
) -> int:
    """Outstanding cell need; unverified counties sort after every cell."""
    if county_key is None:
        return -1
    return cell_need.get((county_key, candidate.category), 0)


def _econ_key(candidate: SchedulableCandidate) -> tuple[int, float]:
    """Tier-3 tiebreak: higher signal first, unknown last."""
    if candidate.economic_signal is None:
        return (1, 0.0)
    return (0, -candidate.economic_signal)


def plan_batch(
    candidates: list[SchedulableCandidate],
    cell_need: Mapping[tuple[str, str], int] | None = None,
    *,
    batch_size: int = BATCH_SIZE,
    group_minimum: int = GROUP_MINIMUM,
    group_maximum: int = GROUP_MAXIMUM,
) -> BatchSchedule:
    """Select one bounded batch of distinct research assignments.

    Fill order honors both priority and protected minimums: high-rise
    (priority 1, capped), verified downtown commercial (priority 2,
    capped, counting toward the commercial minimum), then one merged
    tier-3 pool (non-downtown commercial plus warehouse, econ-tied)
    that both satisfies the minimums and absorbs leftover capacity.
    Maxima bound priority groups to ``2 * group_maximum`` slots, so
    minimums always have room at the default 144 capacity. Downtown
    leftovers above the maximum are preserved, never overflowed.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    if group_minimum < 0 or group_maximum < 1:
        raise ValueError("group minimum/maximum out of range")
    needs = _normalize_cell_need(dict(cell_need or {}))
    distinct = _dedupe(candidates)

    keyed = [(c, _normalize_county(c.county)) for c in distinct]
    highs = [(c, k) for c, k in keyed if c.category == HIGH_RISE]
    downtown = [
        (c, k) for c, k in keyed if c.category == COMMERCIAL and c.is_downtown
    ]
    commercial = [(c, k) for c, k in keyed if c.category == COMMERCIAL]
    warehouses = [(c, k) for c, k in keyed if c.category == WAREHOUSE]

    def need_order(pair: tuple[SchedulableCandidate, str | None]) -> tuple:
        candidate, county_key = pair
        cell = (county_key or "", candidate.category)
        return (-_need_of(candidate, county_key, needs), cell, candidate.candidate_id)

    def tier3_order(pair: tuple[SchedulableCandidate, str | None]) -> tuple:
        candidate, county_key = pair
        cell = (county_key or "", candidate.category)
        return (
            -_need_of(candidate, county_key, needs),
            _econ_key(candidate),
            cell,
            candidate.candidate_id,
        )

    highs.sort(key=need_order)
    downtown.sort(key=need_order)
    commercial.sort(key=tier3_order)
    warehouses.sort(key=tier3_order)

    scheduled: list[tuple[SchedulableCandidate, str | None]] = []
    taken: set[str] = set()

    def take(pool: list[tuple[SchedulableCandidate, str | None]], limit: int) -> None:
        for pair in pool:
            if limit <= 0 or len(scheduled) >= batch_size:
                return
            if pair[0].candidate_id in taken:
                continue
            taken.add(pair[0].candidate_id)
            scheduled.append(pair)
            limit -= 1

    take(highs, group_maximum)
    take(downtown, group_maximum)
    tier3 = [
        pair
        for pair in commercial + warehouses
        if (
            pair[0].candidate_id not in taken
            and not (pair[0].category == COMMERCIAL and pair[0].is_downtown)
        )
    ]
    tier3.sort(key=tier3_order)

    def minima_met() -> tuple[bool, bool]:
        comm = sum(1 for c, _ in scheduled if c.category == COMMERCIAL)
        ware = sum(1 for c, _ in scheduled if c.category == WAREHOUSE)
        return comm >= group_minimum, ware >= group_minimum

    # Walk 1: merged tier-3 order, skipping a subgroup whose minimum is
    # already met while the other minimum still needs capacity.
    for pair in tier3:
        if len(scheduled) >= batch_size:
            break
        if pair[0].candidate_id in taken:
            continue
        ca_met, wh_met = minima_met()
        is_comm = pair[0].category == COMMERCIAL
        if is_comm and ca_met and not wh_met:
            continue
        if not is_comm and wh_met and not ca_met:
            continue
        taken.add(pair[0].candidate_id)
        scheduled.append(pair)
    # Walk 2: overflow — remaining tier-3 in merged order.
    for pair in tier3:
        if len(scheduled) >= batch_size:
            break
        if pair[0].candidate_id in taken:
            continue
        taken.add(pair[0].candidate_id)
        scheduled.append(pair)

    scheduled_ids = tuple(c.candidate_id for c, _ in scheduled)
    preserved = tuple(sorted(c.candidate_id for c in distinct if c.candidate_id not in taken))

    high_n = sum(1 for c, _ in scheduled if c.category == HIGH_RISE)
    down_n = sum(1 for c, _ in scheduled if c.category == COMMERCIAL and c.is_downtown)
    comm_n = sum(1 for c, _ in scheduled if c.category == COMMERCIAL)
    ware_n = sum(1 for c, _ in scheduled if c.category == WAREHOUSE)

    def outcome(scheduled_n: int, eligible_n: int) -> GroupOutcome:
        if scheduled_n >= group_minimum:
            return GroupOutcome(scheduled_n, eligible_n, group_minimum, True, "met")
        if eligible_n < group_minimum:
            return GroupOutcome(scheduled_n, eligible_n, group_minimum, False, "insufficient_work")
        if len(scheduled) < batch_size:
            return GroupOutcome(scheduled_n, eligible_n, group_minimum, False, "scheduling_defect")
        return GroupOutcome(scheduled_n, eligible_n, group_minimum, False, "insufficient_capacity")

    cells: dict[tuple[str, str], int] = {}
    unverified = 0
    for candidate, county_key in scheduled:
        if county_key is None:
            unverified += 1
            continue
        key = (county_key, candidate.category)
        cells[key] = cells.get(key, 0) + 1

    return BatchSchedule(
        scheduled=scheduled_ids,
        preserved=preserved,
        high_rise=high_n,
        downtown_commercial=down_n,
        commercial_anywhere=comm_n,
        warehouse=ware_n,
        commercial_outcome=outcome(comm_n, len(commercial)),
        warehouse_outcome=outcome(ware_n, len(warehouses)),
        cell_assignments=cells,
        unverified_count=unverified,
    )
