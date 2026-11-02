"""Nationwide (USA) source discovery for Gabriel — no preliminary research.

Given only a jurisdiction (city, county, or state name), Gabriel
searches the three free, keyless national open-data catalogs that
index United States government datasets:

* the Socrata catalog (``api.us.socrata.com``),
* ArcGIS Hub (``hub.arcgis.com``),
* data.gov (CKAN, ``catalog.data.gov``).

Hits become :class:`SourceCandidate` records and pass through the
same worldwide qualification policy as every other source: rankings
are computed, publisher authority is never inferred from a search
result, and a generic platform adapter is selected only for a
non-rejected candidate with a machine-readable resource. Catalog
failure is reported honestly (empty ranking plus blockers), never
papered over with invented sources. Bounded: at most one request
per catalog, 12-second timeouts, 1 MB response bound, no paid
provider, no credentials.

This is the deployment path for any US jurisdiction. The DFW pilot
keeps its curated Dallas catalog (``discovery.py``); nothing here
grants registry enrollment or P1 authority.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.parse import urlparse

from app.agents.gabriel.sources.qualification import (
    SourceCandidate,
    rank_sources,
)

MAX_RESPONSE_BYTES = 4_000_000  # ArcGIS Hub search payloads are verbose
TIMEOUT_SECONDS = 12.0

_CATALOGS = ("socrata", "arcgis", "ckan")
_ADAPTER_BY_PLATFORM = {
    "socrata": "generic_socrata",
    "arcgis": "generic_arcgis",
    "ckan": "generic_ckan",
}

HttpGet = Callable[[str, dict, float], Any]


def _default_http_get(url: str, params: dict, timeout: float) -> Any:
    import httpx

    response = httpx.get(
        url, params=params, timeout=timeout,
        headers={"User-Agent": "GrowUp-Gabriel/0.3 (Meta hackathon MVP)"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"catalog_http_{response.status_code}")
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise RuntimeError("catalog_response_too_large")
    return response.json()


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _socrata_candidates(payload: Any, jurisdiction: str) -> list[SourceCandidate]:
    out: list[SourceCandidate] = []
    for item in (payload or {}).get("results", [])[:10]:
        resource = item.get("resource") or {}
        # Live shape (verified 2026-11-02): the portal domain lives in
        # metadata.domain, not on the resource object.
        domain = str((item.get("metadata") or {}).get("domain") or "")
        if not domain:
            continue
        formats = ("json", "csv") if resource.get("type") == "dataset" else ()
        out.append(SourceCandidate(
            name=str(resource.get("name") or domain),
            publisher_name=str(domain), publisher_domain=domain,
            claimed_official=domain.endswith(".gov"),
            jurisdiction=jurisdiction, country="US",
            record_types=("parcels", "appraisal"),
            access_method="api", free=True, requires_auth=False,
            robots_restricted=False, license_clear=False,
            formats=formats, property_level=True, update_age_days=None,
            provenance_url=f"https://{domain}",
        ))
    return out


def _arcgis_candidates(payload: Any, jurisdiction: str) -> list[SourceCandidate]:
    out: list[SourceCandidate] = []
    for item in (payload or {}).get("data", [])[:10]:
        attrs = item.get("attributes") or {}
        url = str(attrs.get("url") or "")
        domain = _host(url) or "hub.arcgis.com"
        out.append(SourceCandidate(
            name=str(attrs.get("name") or domain),
            publisher_name=str(attrs.get("owner") or domain),
            publisher_domain=domain,
            claimed_official=domain.endswith(".gov"),
            jurisdiction=jurisdiction, country="US",
            record_types=("parcels", "gis"),
            access_method="api", free=True, requires_auth=False,
            robots_restricted=False, license_clear=False,
            formats=("geojson", "json"), property_level=True,
            update_age_days=None, provenance_url=url,
        ))
    return out


def _ckan_candidates(payload: Any, jurisdiction: str) -> list[SourceCandidate]:
    out: list[SourceCandidate] = []
    results = ((payload or {}).get("result") or {}).get("results", [])
    for item in results[:10]:
        org = item.get("organization") or {}
        formats = tuple({
            str(r.get("format") or "").lower()
            for r in item.get("resources", [])
            if str(r.get("format") or "").lower() in ("csv", "geojson", "json")
        })
        domain = _host(str(item.get("url") or "")) or "catalog.data.gov"
        out.append(SourceCandidate(
            name=str(item.get("title") or item.get("name") or domain),
            publisher_name=str(org.get("title") or domain),
            publisher_domain=domain,
            claimed_official=domain.endswith(".gov"),
            jurisdiction=jurisdiction, country="US",
            record_types=("parcels", "appraisal"),
            access_method="api" if formats else "portal_search",
            free=True, requires_auth=False, robots_restricted=False,
            license_clear=bool(item.get("license_id")), formats=formats,
            property_level=True, update_age_days=None,
            provenance_url=str(item.get("url") or ""),
        ))
    return out


_PARSERS = {
    "socrata": _socrata_candidates,
    "arcgis": _arcgis_candidates,
    "ckan": _ckan_candidates,
}

_URLS = {
    "socrata": ("https://api.us.socrata.com/api/catalog/v1", "q"),
    "arcgis": ("https://hub.arcgis.com/api/v3/search", "q"),
    # data.gov retired its CKAN API (404 verified 2026-11-02), so the
    # CKAN catalog is used only when an operator configures a live
    # CKAN portal search endpoint (many state portals run CKAN).
    "ckan": (os.environ.get("GABRIEL_CKAN_CATALOG_URL", ""), "q"),
}


def discover_us_sources(
    jurisdiction: str,
    *,
    http_get: HttpGet | None = None,
    record_types: tuple[str, ...] = ("parcels", "appraisal"),
) -> dict[str, Any]:
    """Discover, qualify, and rank US sources for one jurisdiction.

    Zero preliminary research: the only input is the jurisdiction
    name. Transport is injectable for offline tests.
    """
    name = str(jurisdiction or "").strip()
    if not name:
        raise ValueError("jurisdiction is required")
    getter = http_get or _default_http_get
    candidates: list[SourceCandidate] = []
    platforms: dict[str, str] = {}
    blockers: list[str] = []
    for platform in _CATALOGS:
        url, param = _URLS[platform]
        if not url:
            continue  # unconfigured catalog (e.g. CKAN): skipped, not failed
        params = (
            {param: name, "page[size]": 5} if platform == "arcgis"
            else {param: name, "limit": 10}
        )
        try:
            payload = getter(url, params, TIMEOUT_SECONDS)
            found = _PARSERS[platform](payload, name)
        except Exception as exc:  # catalog failure is data, not a crash
            blockers.append(f"{platform} catalog unavailable: {type(exc).__name__}")
            continue
        for candidate in found:
            platforms[candidate.name] = platform
        candidates.extend(found)
    ranked_pairs = rank_sources(candidates, needed_record_types=record_types)
    ranked = [
        {
            "name": c.name, "publisher_domain": c.publisher_domain,
            "platform": platforms.get(c.name),
            "status": q.status, "p1_eligible": q.p1_eligible,
            "score": q.score, "reasons": list(q.reasons),
            "adapter": _ADAPTER_BY_PLATFORM.get(platforms.get(c.name, ""))
            if q.status != "rejected" else None,
        }
        for c, q in ranked_pairs
    ]
    selected = next((r for r in ranked if r["adapter"]), None)
    if not ranked:
        blockers.append("no candidate sources found in any national catalog")
    return {
        "jurisdiction": name,
        "country": "US",
        "ranked": ranked,
        "selected_adapter": selected["adapter"] if selected else None,
        "selected_source": selected["name"] if selected else None,
        "authority_basis": (
            "none: catalog discovery never enrolls a publisher; "
            "registry enrollment remains a separate curated decision"),
        "blockers": blockers,
        "provenance": "Socrata catalog, ArcGIS Hub, data.gov CKAN (free, keyless)",
    }
