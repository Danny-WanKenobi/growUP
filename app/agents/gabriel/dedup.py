"""Duplicate detection for discovered candidates.

Identity rule: a candidate whose normalized street address
(city/state/ZIP when present) matches exactly one stored record refers
to that record; several matches are reported as ambiguous and never
resolved by selection here. Address-less candidates dedup within a run
only, keyed by reliable source identifiers (provider element id, then
coordinates) -- never by name and city alone. Candidates without any
reliable identifier are never deduplicated.

Known limitation: normalization is intentionally simple (case,
punctuation, whitespace). Abbreviation variants ("St" vs "Street")
may not match; Raphael resolves those during verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[.,#\-/]+")


def normalize_text(value: object) -> str:
    """Lowercase, depunctuate, and collapse whitespace ("" when missing)."""
    if value is None:
        return ""
    text = str(value).strip().lower()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def dedup_key(
    payload: dict[str, Any],
    *,
    source: object = None,
    external_id: object = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str | None:
    """Build the in-run dedup key for a candidate, or None when unkeyable.

    Addressed candidates key on the normalized address (identifiers
    ignored). Address-less candidates key on the provider element id,
    then on exact coordinates plus normalized name and city. Name and
    city alone never form a key: without a reliable source identifier
    or coordinates there is nothing to deduplicate on, so None keeps
    every such candidate distinct instead of discarding it.
    """
    street = normalize_text(payload.get("street_address"))
    if street:
        city = normalize_text(payload.get("city"))
        state = normalize_text(payload.get("state"))
        zip_code = normalize_text(payload.get("zip_code"))
        return f"addr:{street}|{city}|{state}|{zip_code}"
    element = str(external_id).strip() if external_id is not None else ""
    if element:
        provider = str(source).strip() if source is not None else ""
        return f"ext:{provider}|{element}"
    if latitude is not None and longitude is not None:
        name = normalize_text(payload.get("name"))
        city = normalize_text(payload.get("city"))
        return f"geo:{latitude!r}|{longitude!r}|{name}|{city}"
    return None


class AddressMatchStatus(str, Enum):
    """Outcome of an operational address lookup (explicit tri-state)."""

    NONE = "none"  # no stored record matches
    UNIQUE = "unique"  # exactly one stored record matches
    AMBIGUOUS = "ambiguous"  # more than one stored record matches


@dataclass(frozen=True)
class AddressMatch:
    """Result of an address lookup. ``property_id`` is set only for UNIQUE."""

    status: AddressMatchStatus
    property_id: UUID | str | None = None


def match_existing_address(payload: dict[str, Any]) -> AddressMatch:
    """Look up operational records with the same street address.

    Returns NONE (no match, or address-less candidate, which dedups
    in-run only), UNIQUE with the matching id, or AMBIGUOUS with no id
    when several records match. Callers must never select among
    multiple matches; the application never updates existing rows from
    discovery, so verified values cannot be overwritten by new estimates.
    """
    from app.properties_db import find_by_address

    if not normalize_text(payload.get("street_address")):
        return AddressMatch(AddressMatchStatus.NONE)
    rows = find_by_address(
        street=normalize_text(payload.get("street_address")),
        city=normalize_text(payload.get("city")),
        state=normalize_text(payload.get("state")),
        zip_code=normalize_text(payload.get("zip_code")),
    )
    if not rows:
        return AddressMatch(AddressMatchStatus.NONE)
    if len(rows) == 1:
        return AddressMatch(AddressMatchStatus.UNIQUE, rows[0]["id"])
    return AddressMatch(AddressMatchStatus.AMBIGUOUS)
