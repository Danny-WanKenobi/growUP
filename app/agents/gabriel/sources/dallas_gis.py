"""City of Dallas GIS enrichment (Gabriel precedence P1, evidence only).

Queries the City of Dallas Sustainable Development & Construction
ArcGIS REST service ``sdc_public/Zoning`` — layer 15, "Base Zoning"
(verified against the live service directory on 2026-11-02) — for
the zoning district containing one point. Zoning is a regulatory
classification claim: it never establishes current use, vacancy,
availability, or permission for any specific activity.

Bounded by construction: one GET per call, 12-second default
timeout, no retries, no redirects beyond the client's default, no
credentials. Transports are injectable for offline testing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from app.agents.gabriel.sources.base import SourceError, SourceTimeout

ZONING_SERVICE_URL = (
    "https://gis.dallascityhall.com/arcgis/rest/services/"
    "sdc_public/Zoning/MapServer/15"
)
ZONING_QUERY_URL = ZONING_SERVICE_URL + "/query"
SOURCE_NAME = "city_of_dallas_gis_zoning"
DEFAULT_TIMEOUT_SECONDS = 12.0

Transport = Callable[[str, dict, float], dict]


def _httpx_transport(url: str, params: dict, timeout: float) -> dict:
    import httpx

    try:
        response = httpx.get(
            url,
            params=params,
            timeout=timeout,
            headers={"User-Agent": "GrowUp-Gabriel/0.3 (Meta hackathon MVP)"},
        )
    except httpx.TimeoutException as exc:
        raise SourceTimeout("dallas_gis_timeout") from exc
    except httpx.HTTPError as exc:
        raise SourceError("dallas_gis_unreachable") from exc
    if response.status_code != 200:
        raise SourceError("dallas_gis_bad_response")
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError("dallas_gis_invalid_json") from exc


def parse_zoning_response(payload: dict, *, latitude: float,
                          longitude: float, retrieved_at: str) -> dict:
    """Normalize one ArcGIS point-query response into a zoning claim."""
    if not isinstance(payload, dict) or not isinstance(
        payload.get("features"), list
    ):
        raise SourceError("dallas_gis_unexpected_response")
    labels: list[str] = []
    attributes: list[dict[str, Any]] = []
    for feature in payload["features"]:
        if not isinstance(feature, dict):
            continue
        attrs = feature.get("attributes") or {}
        if not isinstance(attrs, dict):
            continue
        attributes.append(attrs)
        for key in ("ZONING", "ZONE", "BASE_ZONING", "ZONECLASS"):
            value = attrs.get(key)
            if isinstance(value, str) and value.strip():
                labels.append(value.strip())
                break
    distinct = sorted(set(labels))
    return {
        "source": SOURCE_NAME,
        "service_url": ZONING_SERVICE_URL,
        "latitude": latitude,
        "longitude": longitude,
        "retrieved_at": retrieved_at,
        "zoning_labels": distinct,
        "ambiguous": len(distinct) > 1,
        "attributes": attributes,
        "scope": (
            "City of Dallas base zoning district containing the point; "
            "a regulatory classification only — not current use, "
            "vacancy, availability, or activity permission."
        ),
    }


def zoning_at_point(
    latitude: float,
    longitude: float,
    *,
    transport: Transport | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Return the base-zoning claim for one point (single bounded GET)."""
    params = {
        "geometry": f"{longitude},{latitude}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
    }
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = (transport or _httpx_transport)(
        ZONING_QUERY_URL, params, timeout_seconds
    )
    return parse_zoning_response(
        payload, latitude=latitude, longitude=longitude,
        retrieved_at=retrieved_at,
    )
