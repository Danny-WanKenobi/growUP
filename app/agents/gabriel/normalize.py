"""Normalize raw provider listings into MVP-002 property payloads.

Gabriel populates estimated or unknown fields only -- never verified
fields and never ``field_evidence``. Anything a provider did not supply
stays null; missing information is described in ``notes`` for Raphael's
follow-up instead of being guessed.
"""

from __future__ import annotations

from typing import Any

from app.agents.gabriel.sources.base import RawListing

MISSING_NOTE = (
    "Gabriel preliminary record. Missing (not provided by source): building "
    "area, vacant area, occupancy, ownership, rent, costs, value, liens, "
    "listing duration, vacancy duration, listing history. Long listing or "
    "vacancy periods are not treated as proof of financial distress."
)


def _display_name(item: RawListing) -> str | None:
    if item.name:
        return item.name
    if item.street_address:
        parts = [item.street_address]
        if item.city:
            parts.append(item.city)
        return ", ".join(parts)
    if item.latitude is not None and item.longitude is not None:
        return (
            f"Unnamed {item.category_hint} building at "
            f"{item.latitude:.5f},{item.longitude:.5f} (name unknown)"
        )
    return None


def _provenance(item: RawListing, bbox_label: str) -> str:
    base = (
        f"{item.source} discovery query {bbox_label} retrieved "
        f"{item.retrieved_at} UTC; external ref {item.external_id}"
    )
    if item.element_url:
        base += f" ({item.element_url})"
    return base


def normalize_listing(item: RawListing, bbox_label: str) -> dict[str, Any] | None:
    """Build a PropertyCreate-compatible payload, or None when unusable.

    A candidate without a name, street address, or coordinates cannot
    become a record; it is skipped (never invented around).
    """
    name = _display_name(item)
    if name is None:
        return None
    observed: list[str] = []
    if item.street_address:
        observed.append(f"address {item.street_address}")
    if item.latitude is not None and item.longitude is not None:
        observed.append(f"position {item.latitude:.5f},{item.longitude:.5f}")
    observed.append(f"provider category signal {item.category_hint}")
    notes = f"{MISSING_NOTE} Observed: {'; '.join(observed)}."
    return {
        "name": name[:300],
        "street_address": item.street_address,
        "city": item.city,
        "state": item.state,
        "zip_code": item.zip_code,
        "category": item.category_hint,
        # Estimated numerics stay null: this provider supplies no floor
        # areas, prices, or durations to estimate from. Verified fields
        # are never populated by discovery.
        "occupancy": None,
        "owner": None,
        "provenance": _provenance(item, bbox_label),
        "notes": notes[:5000],
    }
