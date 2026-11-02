"""Overnight mission-control tests: lock, deadline, stop, resume.

Offline only; no provider is contacted and no real mission directory
is touched (all state lives under tmp_path).
"""
from __future__ import annotations

import time

import pytest

from guade.mission import (
    Mission,
    MissionError,
    MissionLock,
    open_mission_ledger,
    preflight,
    snapshot_digest,
    write_authorization_snapshot,
)
from guade.runtime import Authorization

SNAPSHOT = {"mission_id": "overnight-20261102", "billable_enabled": False}


def test_snapshot_is_immutable(tmp_path):
    write_authorization_snapshot(tmp_path, SNAPSHOT)
    digest = snapshot_digest(tmp_path)
    write_authorization_snapshot(tmp_path, SNAPSHOT)
    assert snapshot_digest(tmp_path) == digest
    with pytest.raises(MissionError):
        write_authorization_snapshot(
            tmp_path, {**SNAPSHOT, "billable_enabled": True})


def test_lock_refuses_second_instance(tmp_path):
    first = MissionLock(tmp_path)
    first.acquire()
    try:
        with pytest.raises(MissionError):
            MissionLock(tmp_path).acquire()
    finally:
        first.release()
    MissionLock(tmp_path).acquire()


def test_deadline_and_emergency_stop_fail_closed(tmp_path):
    mission = Mission(tmp_path)
    mission.check_allowed("dispatch")
    mission.state["deadline_at"] = time.time() - 1
    with pytest.raises(MissionError):
        mission.check_allowed("dispatch")
    fresh = Mission(tmp_path / "other")
    fresh.emergency_stop()
    with pytest.raises(MissionError):
        fresh.check_allowed("coach call")


def test_resume_never_replenishes_authorization(tmp_path, monkeypatch):
    monkeypatch.setattr("guade.mission.mission_dir",
                        lambda root: tmp_path / "state-mission")
    auth = Authorization(max_wonder_dispatches=40, max_coach_calls=100)
    ledger = open_mission_ledger(tmp_path, auth)
    ledger.reserve_dispatch()
    reopened = open_mission_ledger(tmp_path, auth)
    assert reopened.counters["wonder_dispatches"] == 1


def test_preflight_verdict_requires_every_check():
    verdict = preflight(__import__("pathlib").Path("."),
                        {"git_clean": True, "meta_billing_verified": False})
    assert verdict["billable_launch_allowed"] is False
    assert verdict["failed"] == ["meta_billing_verified"]
