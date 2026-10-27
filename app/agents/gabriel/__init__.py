"""Gabriel — Property Discovery Agent (MVP-003).

Gabriel is proactive, persistent, resourceful, and evidence-driven. He
discovers real commercial properties that may suit Kratky-hydroponics
conversion and records them as preliminary operational records.

He strictly distinguishes discovering a possible opportunity from
verifying its claims: Gabriel populates estimated or unknown fields
only, never verified fields. Verification belongs to Raphael; Gabriel
preserves every source reference so Raphael can check his work.
"""

from __future__ import annotations

from app.agents.gabriel.schemas import DiscoveryRunRequest, DiscoveryRunResult
from app.agents.gabriel.service import run_discovery

__all__ = ["DiscoveryRunRequest", "DiscoveryRunResult", "run_discovery"]
