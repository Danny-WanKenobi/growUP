"""B5-2A.2b-0a Workstream 2: Big Game Hunter target-balancing scheduler tests.

Pure-function coverage: priority order, batch balancing, geographic
fairness, candidate preservation, and safety boundaries. No I/O, no
autonomous execution, no external calls.
"""

import random

import pytest

from app.agents.gabriel import scheduler
from app.agents.gabriel.scheduler import (
    BATCH_SIZE,
    GROUP_MAXIMUM,
    GROUP_MINIMUM,
    BatchSchedule,
    SchedulableCandidate,
    plan_batch,
)


def _cand(
    cid: str,
    category: str,
    county: str | None = "Dallas County",
    downtown: bool = False,
    econ: float | None = None,
) -> SchedulableCandidate:
    return SchedulableCandidate(
        candidate_id=cid,
        category=category,
        is_downtown=downtown,
        county=county,
        economic_signal=econ,
    )


def _many(prefix: str, n: int, category: str, **kwargs) -> list[SchedulableCandidate]:  # type: ignore[no-untyped-def]
    return [_cand(f"{prefix}-{i:03d}", category, **kwargs) for i in range(n)]


def _abundant() -> list[SchedulableCandidate]:
    return (
        _many("hr", 100, "high_rise")
        + _many("dc", 100, "commercial_property", downtown=True)
        + _many("ca", 100, "commercial_property")
        + _many("wh", 100, "warehouse_industrial")
    )


# --- Priority (1-5) ---


def test_high_rise_first_preference() -> None:
    result = plan_batch(_many("hr", 10, "high_rise") + _many("wh", 10, "warehouse_industrial"))
    assert result.scheduled[:10] == tuple(f"hr-{i:03d}" for i in range(10))


def test_downtown_commercial_second_preference() -> None:
    result = plan_batch(
        _many("ca", 5, "commercial_property")
        + _many("dc", 5, "commercial_property", downtown=True)
    )
    assert result.scheduled[:5] == tuple(f"dc-{i:03d}" for i in range(5))


def test_tier3_remains_eligible() -> None:
    result = plan_batch(
        _many("hr", 50, "high_rise") + _many("ca", 50, "commercial_property")
        + _many("wh", 50, "warehouse_industrial")
    )
    assert result.commercial_anywhere >= GROUP_MINIMUM
    assert result.warehouse >= GROUP_MINIMUM


def test_tier3_tie_uses_economic_signal() -> None:
    cands = [
        _cand("ca-low", "commercial_property", econ=1.0),
        _cand("wh-high", "warehouse_industrial", econ=9.0),
        _cand("ca-mid", "commercial_property", econ=5.0),
    ]
    result = plan_batch(cands)
    assert result.scheduled == ("wh-high", "ca-mid", "ca-low")


def test_unverified_downtown_mention_gets_no_priority() -> None:
    # is_downtown False (uncertain) must not ride the downtown priority lane.
    result = plan_batch(
        [_cand("ca-x", "commercial_property", downtown=False)]
        + _many("dc", 3, "commercial_property", downtown=True)
    )
    assert result.downtown_commercial == 3
    assert result.scheduled[:3] == ("dc-000", "dc-001", "dc-002")
    assert result.scheduled[3] == "ca-x"


# --- Batch balancing (6-15) ---


def test_maxima_and_total_never_exceeded() -> None:
    result = plan_batch(_abundant())
    assert result.high_rise == GROUP_MAXIMUM
    assert result.downtown_commercial == GROUP_MAXIMUM
    assert len(result.scheduled) == BATCH_SIZE
    assert len(set(result.scheduled)) == BATCH_SIZE


def test_minimums_met_with_sufficient_work() -> None:
    result = plan_batch(_abundant())
    assert result.commercial_anywhere >= GROUP_MINIMUM
    assert result.warehouse >= GROUP_MINIMUM
    assert result.commercial_outcome.met and result.commercial_outcome.reason == "met"
    assert result.warehouse_outcome.met and result.warehouse_outcome.reason == "met"


def test_duplicates_consume_one_slot() -> None:
    cand = _cand("dup", "high_rise")
    result = plan_batch([cand, cand, cand])
    assert result.scheduled == ("dup",)
    assert result.high_rise == 1


def test_downtown_counted_once_without_double_scheduling() -> None:
    result = plan_batch(_many("dc", 40, "commercial_property", downtown=True))
    assert result.downtown_commercial == GROUP_MAXIMUM
    assert result.commercial_anywhere == GROUP_MAXIMUM  # inclusive eligibility count
    assert len(result.scheduled) == GROUP_MAXIMUM  # one slot each, no duplicates


def test_shortage_reported_as_insufficient_work() -> None:
    result = plan_batch(_many("wh", 10, "warehouse_industrial"))
    assert result.warehouse == 10
    assert result.warehouse_outcome.met is False
    assert result.warehouse_outcome.reason == "insufficient_work"
    assert result.warehouse_outcome.eligible == 10


def test_unused_capacity_reallocated() -> None:
    # No high-rise or downtown work: all 144 slots go to tier-3 leftovers.
    result = plan_batch(
        _many("ca", 100, "commercial_property") + _many("wh", 100, "warehouse_industrial")
    )
    assert len(result.scheduled) == BATCH_SIZE
    assert result.high_rise == 0 and result.downtown_commercial == 0
    assert result.commercial_anywhere + result.warehouse == BATCH_SIZE


def test_endless_high_rise_cannot_starve_minimums() -> None:
    result = plan_batch(
        _many("hr", 500, "high_rise")
        + _many("ca", 40, "commercial_property")
        + _many("wh", 40, "warehouse_industrial")
    )
    assert result.high_rise == GROUP_MAXIMUM
    assert result.commercial_anywhere == 40
    assert result.warehouse == 40
    assert result.commercial_outcome.met and result.warehouse_outcome.met


# --- Geographic fairness (16-20) ---


def test_all_cells_represented_in_state() -> None:
    needs = {(f"county-{i}", c): 0 for i in range(12) for c in ("x",)}
    with pytest.raises(ValueError):
        plan_batch([], needs)  # unknown counties fail closed
    # All 36 real cells accepted and tracked.
    from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER

    needs = {(county, cat): 4 for county in COUNTY_PRIORITY_ORDER for cat in (
        "high_rise", "commercial_property", "warehouse_industrial")}
    assert len(needs) == 36
    result = plan_batch(_many("hr", 5, "high_rise", county="Dallas County"), needs)
    assert result.cell_assignments == {("dallas county", "high_rise"): 5}


def test_needy_cells_preferred_over_volume() -> None:
    cands = _many("dal", 50, "warehouse_industrial", county="Dallas County") + _many(
        "hunt", 5, "warehouse_industrial", county="Hunt County"
    )
    needs = {("dallas county", "warehouse_industrial"): 0, ("hunt county", "warehouse_industrial"): 4}
    result = plan_batch(cands, needs)
    assert result.scheduled[:5] == tuple(f"hunt-{i:03d}" for i in range(5))


def test_county_city_boundaries_unchanged() -> None:
    from app.agents.gabriel.geography import CITY_PRIORITY_ORDER, COUNTY_PRIORITY_ORDER

    assert len(COUNTY_PRIORITY_ORDER) == 12
    assert all(len(cities) == 5 for cities in CITY_PRIORITY_ORDER.values())


def test_unverified_county_earns_no_quota_credit() -> None:
    result = plan_batch(
        _many("u", 10, "commercial_property", county=None),
        {("dallas county", "commercial_property"): 4},
    )
    assert len(result.scheduled) == 10  # still eligible
    assert result.cell_assignments == {}
    assert result.unverified_count == 10


def test_unsatisfied_cells_stay_eligible() -> None:
    cands = _many("hunt", 3, "high_rise", county="Hunt County")
    needs = {("hunt county", "high_rise"): 4}
    first = plan_batch(cands, needs)
    assert len(first.scheduled) == 3
    # Need outstanding: the same candidates remain schedulable next batch.
    second = plan_batch(cands, {("hunt county", "high_rise"): 1})
    assert len(second.scheduled) == 3


# --- Safety (21-25) ---


def test_unscheduled_candidates_preserved() -> None:
    result = plan_batch(_abundant())
    assert len(result.preserved) == 400 - BATCH_SIZE
    assert set(result.preserved) | set(result.scheduled) == {c.candidate_id for c in _abundant()}
    assert set(result.preserved) & set(result.scheduled) == set()


def test_priority_does_not_alter_qualification_or_scoring() -> None:
    from app.agents.gabriel.ranking import DOWNTOWN_BONUS

    assert DOWNTOWN_BONUS == 0.05  # opportunity formula untouched
    source = scheduler.__file__ or ""
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "from app.agents.gabriel.ranking import" not in text
    assert "downtown_bonus" not in text


def test_no_autonomous_execution_wiring() -> None:
    with open(scheduler.__file__ or "", encoding="utf-8") as handle:
        text = handle.read()
    for primitive in (
        "import threading",
        "import asyncio",
        "from threading",
        "from asyncio",
        "Timer(",
        "sched.scheduler",
        "time.sleep",
        "subprocess",
        "import requests",
    ):
        assert primitive not in text
    import app.agents.gabriel.scheduler as module

    assert not hasattr(module, "run") and not hasattr(module, "start")


def test_deterministic_for_equivalent_inputs() -> None:
    cands = _abundant()
    first = plan_batch(cands)
    shuffled = list(cands)
    random.Random(7).shuffle(shuffled)
    second = plan_batch(shuffled)
    assert first == second


def test_unknown_category_fails_closed() -> None:
    with pytest.raises(ValueError, match="unknown candidate category"):
        plan_batch([_cand("bad", "strip_mall")])
