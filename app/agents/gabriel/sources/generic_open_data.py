"""Generic US open-data collection adapters (Socrata / ArcGIS / CKAN).

These adapters let Gabriel collect property-level records in any US
jurisdiction whose qualified source is a standard open-data platform
— no county-specific code and no preliminary research. The dataset
locator comes from the environment (set by source selection, never
hard-coded):

* ``GENERIC_SOCRATA_URL`` — a Socrata ``/resource/<dataset>.json`` URL
* ``GENERIC_ARCGIS_URL`` — an ArcGIS FeatureServer/MapServer layer URL
* ``GENERIC_CKAN_CSV_URL`` — a direct CSV resource URL from a CKAN portal

Rows are mapped through conservative column aliases; anything a row
does not supply stays unknown. Appraisal-style land-use text is a
category *hint* only, exactly as with the Dallas adapter. Responses
are bounded (row limit owned by the caller, 1 MB body bound,
caller timeout, no retries, no credentials).
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

from app.agents.gabriel.sources.base import (
    BoundingBox,
    RawListing,
    SourceError,
    SourceTimeout,
    register_source,
)

MAX_BODY_BYTES = 1_000_000

_ID_ALIASES = ("account", "parcel_id", "parcelid", "apn", "pin", "id",
               "objectid", "fid", "property_id")
_ADDRESS_ALIASES = ("situs_address", "address", "street_address",
                    "full_address", "addr", "property_address", "location")
_CITY_ALIASES = ("city", "municipality", "town")
_ZIP_ALIASES = ("zip_code", "zip", "zipcode", "postal_code")
_LAT_ALIASES = ("latitude", "lat", "y")
_LON_ALIASES = ("longitude", "lon", "lng", "x")
_USE_ALIASES = ("land_use", "landuse", "use_code", "property_class",
                "classification", "description")

Fetch = Callable[[str, dict, float], bytes]


def _default_fetch(url: str, params: dict, timeout: float) -> bytes:
    import httpx

    try:
        response = httpx.get(
            url, params=params, timeout=timeout,
            headers={"User-Agent": "GrowUp-Gabriel/0.3 (Meta hackathon MVP)"},
        )
    except httpx.TimeoutException as exc:
        raise SourceTimeout("generic_open_data_timeout") from exc
    except httpx.HTTPError as exc:
        raise SourceError("generic_open_data_unreachable") from exc
    if response.status_code != 200:
        raise SourceError("generic_open_data_bad_response")
    if len(response.content) > MAX_BODY_BYTES:
        raise SourceError("generic_open_data_response_too_large")
    return response.content


def _pick(row: dict, aliases: tuple[str, ...]) -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in aliases:
        value = lowered.get(alias)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _float_or_none(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_listings(rows: list[dict], *, source: str, dataset_url: str) -> list[RawListing]:
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    listings: list[RawListing] = []
    seen: set[str] = set()
    for row in rows:
        external = _pick(row, _ID_ALIASES)
        address = _pick(row, _ADDRESS_ALIASES)
        if not external or external in seen:
            continue
        seen.add(external)
        listings.append(RawListing(
            source=source,
            external_id=f"{source}:{external}",
            element_url=None,
            name=None,
            street_address=address or None,
            city=_pick(row, _CITY_ALIASES) or None,
            state=None,
            zip_code=_pick(row, _ZIP_ALIASES) or None,
            category_hint="other",
            latitude=_float_or_none(_pick(row, _LAT_ALIASES)),
            longitude=_float_or_none(_pick(row, _LON_ALIASES)),
            retrieved_at=retrieved_at,
            tags={
                "dataset_url": dataset_url,
                "observed_use": _pick(row, _USE_ALIASES),
                "platform_record": {
                    str(k): str(v)[:200] for k, v in list(row.items())[:20]
                },
            },
        ))
    return listings


def _inside(listings: list[RawListing], bbox: BoundingBox,
            limit: int) -> list[RawListing]:
    south, west, north, east = bbox.as_tuple()
    inside = [
        item for item in listings
        if item.latitude is not None and item.longitude is not None
        and south <= item.latitude <= north
        and west <= item.longitude <= east
    ]
    return (inside or listings)[:limit]


class GenericSocrataSource:
    name = "generic_socrata"
    description = "Generic Socrata SODA dataset collection (any US portal)."
    docs_url = "https://dev.socrata.com/"
    requires_credentials = False

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch

    def search(self, bbox: BoundingBox, categories: list[str],
               limit: int, timeout_seconds: float) -> list[RawListing]:
        url = os.environ.get("GENERIC_SOCRATA_URL", "").strip()
        if not url:
            raise SourceError("generic_socrata_not_configured")
        body = self._fetch(url, {"$limit": str(max(1, limit))},
                           timeout_seconds)
        try:
            rows = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceError("generic_socrata_invalid_json") from exc
        if not isinstance(rows, list):
            raise SourceError("generic_socrata_unexpected_response")
        return _inside(_to_listings(rows, source=self.name, dataset_url=url),
                       bbox, limit)


class GenericArcGISSource:
    name = "generic_arcgis"
    description = "Generic ArcGIS FeatureServer layer collection (any US portal)."
    docs_url = "https://developers.arcgis.com/rest/"
    requires_credentials = False

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch

    def search(self, bbox: BoundingBox, categories: list[str],
               limit: int, timeout_seconds: float) -> list[RawListing]:
        url = os.environ.get("GENERIC_ARCGIS_URL", "").strip()
        if not url:
            raise SourceError("generic_arcgis_not_configured")
        south, west, north, east = bbox.as_tuple()
        params = {
            "where": "1=1", "outFields": "*",
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "outSR": "4326", "f": "geojson",
            "resultRecordCount": str(max(1, limit)),
        }
        body = self._fetch(url.rstrip("/") + "/query", params, timeout_seconds)
        try:
            payload = json.loads(body.decode("utf-8"))
            rows = [
                dict(feature.get("properties") or {})
                for feature in payload.get("features", [])
            ]
        except (ValueError, UnicodeDecodeError, AttributeError) as exc:
            raise SourceError("generic_arcgis_invalid_response") from exc
        return _inside(_to_listings(rows, source=self.name, dataset_url=url),
                       bbox, limit)


class GenericCKANSource:
    name = "generic_ckan"
    description = "Generic CKAN CSV resource collection (any US portal)."
    docs_url = "https://docs.ckan.org/"
    requires_credentials = False

    def __init__(self, fetch: Fetch | None = None) -> None:
        self._fetch = fetch or _default_fetch

    def search(self, bbox: BoundingBox, categories: list[str],
               limit: int, timeout_seconds: float) -> list[RawListing]:
        url = os.environ.get("GENERIC_CKAN_CSV_URL", "").strip()
        if not url:
            raise SourceError("generic_ckan_not_configured")
        body = self._fetch(url, {}, timeout_seconds)
        try:
            rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise SourceError("generic_ckan_invalid_csv") from exc
        return _inside(_to_listings(rows, source=self.name, dataset_url=url),
                       bbox, limit)


register_source(GenericSocrataSource())
register_source(GenericArcGISSource())
register_source(GenericCKANSource())
