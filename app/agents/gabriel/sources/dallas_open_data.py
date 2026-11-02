"""Dallas Open Data (Socrata) enrichment (Gabriel precedence P1).

Bounded SODA queries against City of Dallas open datasets. The
default dataset is "Base Zoning" (``f3t3-a5rd``), verified through
the portal's ``data.json`` catalog on 2026-11-02. These datasets are
evidence enrichment only: a zoning or permit record never
establishes current use, vacancy, or availability.

One GET per call, explicit ``$limit``/``$offset`` pagination owned
by the caller, 12-second default timeout, no retries, no
credentials (an optional Socrata app token may be supplied by the
operator for rate headroom; it is never required or logged).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.agents.gabriel.sources.base import SourceError, SourceTimeout

PORTAL_URL = "https://www.dallasopendata.com"
BASE_ZONING_DATASET = "f3t3-a5rd"
SOURCE_NAME = "dallas_open_data"
DEFAULT_TIMEOUT_SECONDS = 12.0
MAX_LIMIT = 500

Transport = Callable[[str, dict, float], list]


def _httpx_transport(url: str, params: dict, timeout: float) -> list:
    import httpx

    try:
        response = httpx.get(
            url,
            params=params,
            timeout=timeout,
            headers={"User-Agent": "GrowUp-Gabriel/0.3 (Meta hackathon MVP)"},
        )
    except httpx.TimeoutException as exc:
        raise SourceTimeout("dallas_open_data_timeout") from exc
    except httpx.HTTPError as exc:
        raise SourceError("dallas_open_data_unreachable") from exc
    if response.status_code != 200:
        raise SourceError("dallas_open_data_bad_response")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError("dallas_open_data_invalid_json") from exc
    if not isinstance(payload, list):
        raise SourceError("dallas_open_data_unexpected_response")
    return payload


def query_dataset(
    dataset_id: str = BASE_ZONING_DATASET,
    *,
    where: str | None = None,
    limit: int = 100,
    offset: int = 0,
    transport: Transport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Run one bounded SODA query and return rows with provenance."""
    if not dataset_id or not dataset_id.replace("-", "").isalnum():
        raise SourceError("dallas_open_data_bad_dataset")
    if limit < 1 or offset < 0:
        raise SourceError("dallas_open_data_bad_pagination")
    params: dict[str, str] = {
        "$limit": str(min(limit, MAX_LIMIT)),
        "$offset": str(offset),
    }
    if where:
        params["$where"] = where
    url = f"{PORTAL_URL}/resource/{dataset_id}.json"
    rows = (transport or _httpx_transport)(url, params, timeout_seconds)
    return {
        "source": SOURCE_NAME,
        "dataset_id": dataset_id,
        "resource_url": url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "limit": min(limit, MAX_LIMIT),
        "offset": offset,
        "rows": rows,
        "scope": (
            "City of Dallas open-data records; evidence enrichment "
            "only — not current use, vacancy, or availability."
        ),
    }
