"""Official OpenStreetMap API v0.6 discovery provider (no credentials).

Uses the read-only map endpoint (``GET /api/0.6/map``) for small,
controlled bounding boxes. This source provides mapped building
geometry and tags -- not commercial listings: no prices, vacancy,
ownership, or listing history. Those unknowns stay null downstream.

The official API is a shared editing resource, not a bulk-harvesting
service, so this provider enforces conservative geographic limits of
its own, makes exactly one request per search (no retries, no
subdivision), and raises instead of fabricating on any failure.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from app.agents.gabriel.sources.base import (
    BoundingBox,
    RawListing,
    SourceError,
    SourceTimeout,
    register_source,
)
from app.agents.gabriel.sources.overpass import USER_AGENT, map_category

logger = logging.getLogger(__name__)

OSM_API_MAP_URL = "https://api.openstreetmap.org/api/0.6/map"
OSM_API_DOCS = "https://wiki.openstreetmap.org/wiki/API_v0.6"

# Conservative provider-specific limits (far below the API's own caps).
# The discovery API may allow larger boxes for other providers; osm_api
# rejects anything above these before any HTTP request is made.
MAX_BBOX_SPAN_DEGREES = 0.05
MAX_BBOX_AREA_SQ_DEGREES = 0.001


def check_bbox(bbox: BoundingBox) -> None:
    """Reject oversized boxes before any network request is made."""
    south, west, north, east = bbox.as_tuple()
    lat_span = north - south
    lon_span = east - west
    area = lat_span * lon_span
    if (
        lat_span <= 0
        or lon_span <= 0
        or lat_span > MAX_BBOX_SPAN_DEGREES
        or lon_span > MAX_BBOX_SPAN_DEGREES
        or area > MAX_BBOX_AREA_SQ_DEGREES
    ):
        raise SourceError(
            "osm_api_bbox_too_large: keep requests at or below "
            f"{MAX_BBOX_SPAN_DEGREES} degrees per side and "
            f"{MAX_BBOX_AREA_SQ_DEGREES} square degrees"
        )


def _tags_of(element: ET.Element) -> dict[str, str]:
    tags: dict[str, str] = {}
    for tag in element.findall("tag"):
        key = tag.get("k")
        value = tag.get("v")
        if key:
            tags[key] = value or ""
    return tags


def _address_parts(tags: dict[str, str]) -> tuple[str | None, str | None, str | None, str | None]:
    house = tags.get("addr:housenumber", "").strip()
    street = tags.get("addr:street", "").strip()
    return (
        f"{house} {street}".strip() or None,
        tags.get("addr:city", "").strip() or None,
        tags.get("addr:state", "").strip() or None,
        tags.get("addr:postcode", "").strip() or None,
    )


def _parse_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def parse_osm_xml(text: str, retrieved_at: str) -> list[RawListing]:
    """Convert OSM API map XML to observed raw listings.

    Eligible elements carry a real ``building`` tag (``building=no`` and
    bare ``building:part`` fragments are skipped: parts are not treated
    as independent properties). Way coordinates are the mean of the
    referenced nodes present in the response; when none are present the
    coordinates stay None rather than being guessed.
    """
    try:
        root = ET.fromstring(text or "")
    except ET.ParseError as exc:
        raise SourceError("osm_api_invalid_xml") from exc
    if root.tag != "osm":
        raise SourceError("osm_api_unexpected_response")

    nodes: dict[str, tuple[float | None, float | None, dict[str, str]]] = {}
    for node in root.findall("node"):
        node_id = node.get("id")
        if node_id is None:
            continue
        nodes[node_id] = (
            _parse_float(node.get("lat")),
            _parse_float(node.get("lon")),
            _tags_of(node),
        )

    listings: list[RawListing] = []

    def _eligible(tags: dict[str, str]) -> bool:
        building = tags.get("building", "").strip().lower()
        if building and building != "no":
            return True
        return False  # bare building:part fragments and untagged geometry excluded

    def _build(
        element_type: str,
        element_id: str,
        tags: dict[str, str],
        lat: float | None,
        lon: float | None,
    ) -> None:
        street, city, state, zip_code = _address_parts(tags)
        listings.append(
            RawListing(
                source="osm_api",
                external_id=f"{element_type}/{element_id}",
                element_url=f"https://www.openstreetmap.org/{element_type}/{element_id}",
                name=tags.get("name", "").strip() or None,
                street_address=street,
                city=city,
                state=state,
                zip_code=zip_code,
                category_hint=map_category(tags),
                latitude=lat,
                longitude=lon,
                retrieved_at=retrieved_at,
                tags=dict(tags),
            )
        )

    for node in root.findall("node"):
        node_id = node.get("id")
        if node_id is None:
            continue
        lat, lon, tags = nodes[node_id]
        if _eligible(tags):
            _build("node", node_id, tags, lat, lon)

    for way in root.findall("way"):
        way_id = way.get("id")
        if way_id is None:
            continue
        tags = _tags_of(way)
        if not _eligible(tags):
            continue
        latitudes: list[float] = []
        longitudes: list[float] = []
        for nd in way.findall("nd"):
            ref = nd.get("ref")
            if ref is not None and ref in nodes:
                lat, lon, _ = nodes[ref]
                if lat is not None and lon is not None:
                    latitudes.append(lat)
                    longitudes.append(lon)
        if latitudes:
            lat = sum(latitudes) / len(latitudes)
            lon = sum(longitudes) / len(longitudes)
        else:
            lat, lon = None, None
        _build("way", way_id, tags, lat, lon)

    return listings


class OsmApiSource:
    """Mapped-building discovery via the official OSM API v0.6."""

    name = "osm_api"
    description = (
        "OpenStreetMap building geometry and tags from the official API map "
        "endpoint (small boxes only). Provides mapped buildings, not "
        "commercial listings: no prices, vacancy, or ownership data."
    )
    docs_url = OSM_API_DOCS
    requires_credentials = False

    def search(
        self,
        bbox: BoundingBox,
        categories: list[str],
        limit: int,
        timeout_seconds: float,
    ) -> list[RawListing]:
        import httpx

        check_bbox(bbox)  # never spend a request on an oversized box
        south, west, north, east = bbox.as_tuple()
        params = {"bbox": f"{west},{south},{east},{north}"}
        retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            response = httpx.get(
                OSM_API_MAP_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise SourceTimeout(f"osm_api_timeout after {timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise SourceError("osm_api_unreachable") from exc
        if response.status_code != 200:
            logger.warning("osm_api map query failed: HTTP %s", response.status_code)
            raise SourceError("osm_api_bad_response")
        listings = parse_osm_xml(response.text, retrieved_at)
        # Preserve response order (no category ranking); filter, then limit.
        wanted = set(categories)
        return [item for item in listings if item.category_hint in wanted][:limit]


register_source(OsmApiSource())
