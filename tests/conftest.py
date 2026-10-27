"""Shared pytest fixtures (no live services, no ambient side effects)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_ambient_observability(monkeypatch):
    """Keep Gabriel tests from touching real log files or PostgreSQL.

    Ambient instrumentation emits become validate-and-discard. Tests
    that assert on sinks pass explicit configs/sinks (always enabled)
    or monkeypatch the emitter directly.
    """
    monkeypatch.setenv("OBS_ENABLED", "0")
