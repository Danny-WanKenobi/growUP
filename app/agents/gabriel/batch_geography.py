"""Dynamic batch geography for Gabriel's 144-dossier batches.

Owner directive (2026-11-02), permanently installed: batch geography
is *derived*, never hard-coded to Dallas. For a requested state
Gabriel resolves, from the versioned population dataset in
``config/batch_geography.json`` (2020 Census basis):

1. the most populated city in the state;
2. the 12 most populated counties in that city's metropolitan area
   (OMB metropolitan + combined statistical area counties);
3. a diversity matrix of 4 candidates x 3 canonical property
   categories x 12 counties = 144 dossiers per batch.

The legacy Dallas/DFW roster (``app.agents.gabriel.geography``) and
the existing DFW pilot ledger remain exactly as they are for Dallas
searches and the first pilot batch already in flight; this module
governs how *new* batches choose geography. Dallas appears here only
when the derivation itself selects it (it does not for Texas: Houston
is the most populated city). Rankings are computed from the dataset;
refreshing the dataset from a new Census release changes future
batches without code changes. Unknown states, missing metros, or
metros with fewer than 12 counties fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DATASET_PATH = Path(__file__).resolve().parents[3] / "config" / "batch_geography.json"

CATEGORIES = ("high_rise", "commercial_property", "warehouse_industrial")
COUNTY_COUNT = 12
CANDIDATES_PER_CELL = 4


class BatchGeographyError(ValueError):
    """The requested batch geography cannot be derived from the dataset."""


def _load_dataset() -> dict[str, Any]:
    try:
        data = json.loads(_DATASET_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchGeographyError("batch geography dataset unavailable") from exc
    # Additional states are data, not code: drop a Census-derived
    # file into config/batch_geography.d/<STATE>.json (same schema)
    # and the state resolves with no code change.
    extra_dir = _DATASET_PATH.parent / "batch_geography.d"
    try:
        extra_files = sorted(extra_dir.glob("*.json"))
    except OSError:
        extra_files = []
    states = data.setdefault("states", {})
    for path in extra_files:
        try:
            extra = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for state, entry in (extra.get("states") or {}).items():
            states.setdefault(state, entry)
    return data


def principal_city(state: str, dataset: dict[str, Any] | None = None) -> dict[str, Any]:
    """The most populated city in the state (computed, not named)."""
    data = dataset if dataset is not None else _load_dataset()
    entry = (data.get("states") or {}).get(str(state or "").strip().upper())
    if not entry or not entry.get("cities_by_population"):
        raise BatchGeographyError(f"no city population data for state: {state!r}")
    return max(entry["cities_by_population"], key=lambda c: c["population_2020"])


def metro_counties(
    state: str, *, top_n: int = COUNTY_COUNT, dataset: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The ``top_n`` most populated counties of the principal metro."""
    data = dataset if dataset is not None else _load_dataset()
    city = principal_city(state, data)
    entry = data["states"][str(state).strip().upper()]
    metro = (entry.get("metros") or {}).get(city["name"])
    if not metro or not metro.get("counties_by_population"):
        raise BatchGeographyError(
            f"no metropolitan county data for {city['name']!r} ({state!r})")
    ranked = sorted(
        metro["counties_by_population"],
        key=lambda c: (-int(c["population_2020"]), str(c["county"])),
    )
    if len(ranked) < top_n:
        raise BatchGeographyError(
            f"metropolitan area for {city['name']!r} has only "
            f"{len(ranked)} counties; {top_n} required")
    return ranked[:top_n]


def batch_cells(
    state: str = "TX", *, dataset: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """The 36-cell diversity matrix (12 counties x 3 categories)."""
    counties = metro_counties(state, dataset=dataset)
    return [
        {
            "county": county["county"],
            "category": category,
            "target": CANDIDATES_PER_CELL,
            "county_population_2020": int(county["population_2020"]),
        }
        for county in counties
        for category in CATEGORIES
    ]


def batch_plan(state: str = "TX", *, dataset: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full derivation record for one 144-dossier batch."""
    data = dataset if dataset is not None else _load_dataset()
    city = principal_city(state, data)
    cells = batch_cells(state, dataset=data)
    return {
        "state": str(state).strip().upper(),
        "principal_city": city["name"],
        "principal_city_population_2020": int(city["population_2020"]),
        "population_basis": data.get("population_basis", ""),
        "counties": [cell["county"] for cell in cells[::len(CATEGORIES)]],
        "cells": cells,
        "cell_count": len(cells),
        "target_total": sum(cell["target"] for cell in cells),
    }


def legacy_dfw_cells() -> list[dict[str, Any]]:
    """The existing DFW roster as batch cells (Dallas searches only).

    Preserved for the in-flight DFW pilot batch and Dallas-specific
    work; new batches derive geography via :func:`batch_cells`.
    """
    from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER

    return [
        {"county": county, "category": category, "target": CANDIDATES_PER_CELL}
        for county in COUNTY_PRIORITY_ORDER
        for category in CATEGORIES
    ]
