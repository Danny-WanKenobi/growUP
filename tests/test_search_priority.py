"""Permanent search-priority + Gabriel demo/live parity tests."""
from __future__ import annotations

from app.agents.gabriel.search_priority import advance, front_at, ordered_fronts


def test_dfw_first_in_existing_county_order():
    fronts = ordered_fronts()
    assert fronts[0]["metro"] == "Dallas-Fort Worth"
    assert fronts[0]["counties"][0] == "dallas county"
    assert len(fronts[0]["counties"]) == 12
    assert fronts[0]["counties_status"] == "resolved"


def test_houston_second_then_texas_metros_descending():
    fronts = ordered_fronts()
    assert fronts[1]["metro"] == "Houston"
    assert fronts[1]["counties"][0] == "harris county"
    assert len(fronts[1]["counties"]) == 12
    texas = [f["metro"] for f in fronts if f["state"] == "TX"]
    assert texas[:4] == ["Dallas-Fort Worth", "Houston", "San Antonio", "Austin"]
    assert texas[-1] == "Tyler"


def test_states_follow_texas_by_population_descending():
    fronts = ordered_fronts()
    states = [f["state"] for f in fronts if f["state"] != "TX"]
    assert states[0] == "California"
    assert states[-1] == "Wyoming"
    california = next(f for f in fronts if f["state"] == "California")
    assert california["metro"] == "Los Angeles"


def test_sequence_restarts_at_dfw_next_cycle():
    fronts = ordered_fronts()
    wrapped = front_at(len(fronts) + 1)
    assert wrapped["metro"] == "Dallas-Fort Worth"
    assert wrapped["cycle"] == 2
    assert front_at(1)["cycle"] == 1


def test_advance_requires_attempt_completion():
    assert advance(3, attempt_complete=False) == 3
    assert advance(3, attempt_complete=True) == 4


def test_gabriel_discovery_identical_in_demo_and_live(monkeypatch):
    # Parity contract: Gabriel's discovery, qualification, and
    # geography derivation must not branch on DEMO_MODE. (Gabriel's
    # stack contains no DEMO_MODE reference at all; this test pins
    # the observable behavior so a future branch fails loudly.)
    from app.agents.gabriel.batch_geography import batch_plan
    from app.agents.gabriel.sources.discovery import discover_sources

    outcomes = {}
    for mode in ("yes", "no"):
        monkeypatch.setenv("DEMO_MODE", mode)
        outcomes[mode] = (
            discover_sources("tarrant county", "high_rise"),
            batch_plan("TX")["counties"],
        )
    assert outcomes["yes"] == outcomes["no"]
