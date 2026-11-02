"""Gabriel's permanent nationwide search priority (owner directive).

Sequence, permanently installed (2026-11-02):

1. **DFW Metroplex** — county by county in the existing DFW order
   (``geography.COUNTY_PRIORITY_ORDER``, Dallas County first).
2. **Houston Metroplex** — the 12 most populated Greater Houston
   counties (dynamic batch geography derivation).
3. **Remaining Texas metros** — by principal-city population,
   descending (San Antonio, Austin, El Paso, ...).
4. **Every remaining state** — states by 2020 Census population,
   descending (Texas already covered), each front being that state's
   most populous city's metroplex.
5. **Restart** — after the whole USA has been attempted, begin again
   at DFW with the next cycle.

A front is *attempt-complete* when its batch quota is met or its
legitimate sources are exhausted (owner-selected restart trigger);
completion never requires fabricating dossiers. Ordering data lives
in ``config/search_priority.json`` (2020 Census basis) so a Census
refresh re-orders future cycles without code changes. County sets
resolve through the batch-geography datasets; a front whose metro
county dataset is not yet present reports
``geography_data_required`` instead of inventing counties — the
*order* is permanent even where data collection is still catching
up.

This module is Gabriel policy only: it names where to search next.
It does not change source precedence (P1 official, P2 SearXNG,
P3 Brave), qualification, or any agent boundary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATASET_PATH = Path(__file__).resolve().parents[3] / "config" / "search_priority.json"


class SearchPriorityError(ValueError):
    """The permanent search-priority dataset is unavailable."""


def _load_dataset() -> dict[str, Any]:
    try:
        return json.loads(_DATASET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SearchPriorityError("search priority dataset unavailable") from exc


def _front(position: int, *, state: str, metro: str,
           counties: list[str] | None) -> dict[str, Any]:
    return {
        "position": position,
        "state": state,
        "metro": metro,
        "counties": counties,
        "counties_status": "resolved" if counties else "geography_data_required",
    }


def ordered_fronts(dataset: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """All search fronts in permanent priority order (one USA cycle)."""
    from app.agents.gabriel.batch_geography import metro_counties
    from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER, display_name

    data = dataset if dataset is not None else _load_dataset()
    fronts: list[dict[str, Any]] = []
    fronts.append(_front(1, state="TX", metro="Dallas-Fort Worth",
                         counties=list(COUNTY_PRIORITY_ORDER)))
    houston = [str(c["county"]) for c in metro_counties("TX")]
    fronts.append(_front(2, state="TX", metro="Houston", counties=houston))
    position = 2
    for metro in data.get("texas_metros_after_houston", []):
        position += 1
        fronts.append(_front(position, state="TX",
                             metro=str(metro["principal_city"]),
                             counties=None))
    for entry in data.get("states_by_population", []):
        if entry["state"] == "Texas":
            continue  # Texas is covered by the pinned fronts above
        position += 1
        fronts.append(_front(position, state=str(entry["state"]),
                             metro=str(entry["principal_city"]),
                             counties=None))
    return fronts


def front_at(position: int, dataset: dict[str, Any] | None = None) -> dict[str, Any]:
    """The front for a 1-based sequence position (wraps into cycles).

    Position ``len(fronts) + 1`` is DFW again, cycle 2 — the
    permanent restart rule.
    """
    if position < 1:
        raise SearchPriorityError("position must be >= 1")
    fronts = ordered_fronts(dataset)
    index = (position - 1) % len(fronts)
    cycle = (position - 1) // len(fronts) + 1
    return {**fronts[index], "position": position, "cycle": cycle}


def advance(position: int, *, attempt_complete: bool) -> int:
    """Next position: advance only when the current front is
    attempt-complete (quota met or sources exhausted)."""
    if position < 1:
        raise SearchPriorityError("position must be >= 1")
    return position + 1 if attempt_complete else position
