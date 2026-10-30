"""OpenStreetMap Overpass discovery provider (no credentials required).

Uses the public Overpass API (https://overpass-api.de) with documented
query language over HTTPS. Complies with the OSM usage policy: bounded
result counts, explicit timeouts, and an identifying User-Agent. No
authentication, CAPTCHA, or paywall is bypassed; on any failure this
module raises instead of fabricating listings.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.agents.gabriel.sources.base import (
    BoundingBox,
    RawListing,
    SourceError,
    SourceTimeout,
    register_source,
)

logger = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_DOCS = "https://wiki.openstreetmap.org/wiki/Overpass_API"
USER_AGENT = "GrowUp-Gabriel/0.3 (Meta hackathon MVP; https://github.com/Danny-WanKenobi/growUP)"

# Small downtown Dallas window for a first controlled discovery test.
DALLAS_DOWNTOWN_BBOX = BoundingBox(south=32.770, west=-96.810, north=32.790, east=-96.785)

_WAREHOUSE_TAGS = {"warehouse", "industrial"}
_COMMERCIAL_TAGS = {"commercial", "retail", "office"}
_HIGH_RISE_LEVELS = 8
_HIGH_RISE_HEIGHT_M = 25.0


def build_query(bbox: BoundingBox, count: int, timeout_seconds: float) -> str:
    """Build a bounded Overpass QL query for candidate buildings."""
    south, west, north, east = bbox.as_tuple()
    return (
        f"[out:json][timeout:{int(timeout_seconds)}];"
        "("
        'nwr["building"~"^(commercial|retail|office|warehouse|industrial|skyscraper)$"]'
        f"({south},{west},{north},{east});"
        'nwr["building"]["building:levels"]'
        f"({south},{west},{north},{east});"
        ");"
        f"out center {count};"
    )


def _parse_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip().split()[0].rstrip("m"))
    except (ValueError, IndexError):
        return None


def _parse_int(value: object) -> int | None:
    number = _parse_float(value)
    return int(number) if number is not None else None


def map_category(tags: dict) -> str:
    """Map OSM building tags to one canonical discovery category.

    All three discovery categories are queried together with no ranking
    boost; anything unclear maps to ``other`` rather than being forced
    into scope.
    """
    building = str(tags.get("building", "")).strip().lower()
    if building in _WAREHOUSE_TAGS:
        return "warehouse_industrial"
    levels = _parse_int(tags.get("building:levels"))
    height = _parse_float(tags.get("height"))
    if building == "skyscraper" or (
        levels is not None and levels >= _HIGH_RISE_LEVELS
    ) or (height is not None and height >= _HIGH_RISE_HEIGHT_M):
        return "high_rise"
    if building in _COMMERCIAL_TAGS:
        return "commercial_property"
    return "other"


def _element_position(element: dict) -> tuple[float | None, float | None]:
    if isinstance(element.get("lat"), (int, float)) and isinstance(element.get("lon"), (int, float)):
        return float(element["lat"]), float(element["lon"])
    center = element.get("center") or {}
    lat, lon = center.get("lat"), center.get("lon")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return None, None


def parse_elements(payload: dict, retrieved_at: str) -> list[RawListing]:
    """Convert an Overpass JSON payload to observed raw listings."""
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise SourceError("overpass_unexpected_response")
    listings: list[RawListing] = []
    for element in payload["elements"]:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            tags = {}
        element_type = str(element.get("type", "node"))
        element_id = element.get("id")
        if element_id is None:
            continue
        house = str(tags.get("addr:housenumber", "")).strip()
        street = str(tags.get("addr:street", "")).strip()
        lat, lon = _element_position(element)
        listings.append(
            RawListing(
                source="overpass",
                external_id=f"{element_type}/{element_id}",
                element_url=f"https://www.openstreetmap.org/{element_type}/{element_id}",
                name=str(tags["name"]).strip() if tags.get("name") else None,
                street_address=f"{house} {street}".strip() or None,
                city=str(tags.get("addr:city", "")).strip() or None,
                state=str(tags.get("addr:state", "")).strip() or None,
                zip_code=str(tags.get("addr:postcode", "")).strip() or None,
                category_hint=map_category(tags),
                latitude=lat,
                longitude=lon,
                retrieved_at=retrieved_at,
                tags={k: v for k, v in tags.items() if isinstance(k, str)},
            )
        )
    return listings


class OverpassSource:
    """Real building discovery via the public Overpass API."""

    name = "overpass"
    description = (
        "OpenStreetMap building footprints with addresses and building-type "
        "tags (no listing prices or vacancy data)."
    )
    docs_url = OVERPASS_DOCS
    requires_credentials = False

    def search(
        self,
        bbox: BoundingBox,
        categories: list[str],
        limit: int,
        timeout_seconds: float,
    ) -> list[RawListing]:
        import httpx

        wanted = set(categories)
        # Request headroom: unusable elements are skipped during normalization.
        count = min(max(limit * 3, 10), 100)
        query = build_query(bbox, count, timeout_seconds)
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            response = httpx.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SourceTimeout(f"overpass_timeout after {timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            # Network failure: raise, never fabricate a fallback.
            raise SourceError("overpass_unreachable") from exc
        if response.status_code != 200:
            logger.warning("overpass query failed: HTTP %s", response.status_code)
            raise SourceError("overpass_bad_response")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("overpass_invalid_json") from exc
        listings = parse_elements(payload, retrieved_at)
        # Preserve provider order (no category ranking); apply filter, then limit.
        filtered = [item for item in listings if item.category_hint in wanted]
        return filtered[:limit]


register_source(OverpassSource())
