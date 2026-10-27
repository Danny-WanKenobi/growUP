"""Duplicate detection for discovered candidates.

Identity rule: two records with the same normalized street address
(city/state/ZIP when present) are the same property. Address-less
candidates dedup within a run only, keyed by name and city, and are
flagged for Raphael's review.

Known limitation: normalization is intentionally simple (case,
punctuation, whitespace). Abbreviation variants ("St" vs "Street")
may not match; Raphael resolves those during verification.
"""

from __future__ import annotations

import re
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


def dedup_key(payload: dict[str, Any]) -> str:
    """Build the identity key for a normalized candidate payload."""
    street = normalize_text(payload.get("street_address"))
    if street:
        city = normalize_text(payload.get("city"))
        state = normalize_text(payload.get("state"))
        zip_code = normalize_text(payload.get("zip_code"))
        return f"addr:{street}|{city}|{state}|{zip_code}"
    name = normalize_text(payload.get("name"))
    city = normalize_text(payload.get("city"))
    return f"name:{name}|{city}"


def find_existing_id(payload: dict[str, Any]) -> UUID | str | None:
    """Look up an operational record with the same street address.

    Returns the existing id or None. Address-less candidates return
    None here (in-run dedup only); the application never updates
    existing rows from discovery, so verified values cannot be
    overwritten by new estimates.
    """
    from app.properties_db import find_by_address

    if not normalize_text(payload.get("street_address")):
        return None
    row = find_by_address(
        street=normalize_text(payload.get("street_address")),
        city=normalize_text(payload.get("city")),
        state=normalize_text(payload.get("state")),
        zip_code=normalize_text(payload.get("zip_code")),
    )
    return row["id"] if row else None
