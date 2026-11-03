"""Nationwide (US) source discovery: thin compatibility wrapper.

This module is *not* a discovery engine. It builds one
:class:`SourceMission` over the configured national P1 catalogs
(Socrata catalog, ArcGIS Hub, and a CKAN catalog only when
``GABRIEL_CKAN_CATALOG_URL`` configures a live portal) and delegates
to the existing shared durable path
(``discovery.select_for_mission`` -> ``source_finder.find_candidates``
-> shared qualification/ranking -> ``source_selection.select_sources``).

Consequences, by construction:

- Default (unapproved, zero request budget) cannot network. Real
  finding requires a stable ``assignment_key`` with a committed
  durable claim before transmission (test-injected transports may
  use the in-process path, exactly as ``select_for_mission`` allows).
- Catalog observations never enroll a trusted publisher, never
  authorize property facts, and never infer jurisdiction, parcel
  coverage, publisher authenticity or license clearance from the
  query or a nonempty license id. Unknown terms/coverage/freshness
  stay unknown and route to ``needs_review``; a ``needs_review``
  candidate is never selected for acquisition.
- Qwen (and every model) is excluded from Gabriel finding/ranking:
  this path is deterministic code only.
- The DFW pilot keeps its curated Dallas catalog; nothing here is a
  Dallas-only or worldwide hard-coded source list.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from app.agents.gabriel.sources.source_selection import (
    DiscoveryBudget,
    SourceMission,
    SourceSelectionError,
)

TIMEOUT_SECONDS = 12.0

HttpGet = Callable[..., Any]

_CATALOGS = (
    ("socrata_catalog", "https://api.us.socrata.com/api/catalog/v1", "socrata"),
    ("arcgis_catalog", "https://hub.arcgis.com/api/v3/search", "arcgis"),
)


def _wrap_http_get(http_get: HttpGet | None) -> HttpGet | None:
    """Normalize injected transports to the finder response shape."""
    if http_get is None:
        return None

    def getter(url, params=None, timeout=None):  # type: ignore[no-untyped-def]
        resp = http_get(url, params, timeout)
        if isinstance(resp, dict) and "status_code" not in resp and "content" not in resp:
            return {"status_code": 200, "headers": {},
                    "content": json.dumps(resp).encode()}
        return resp

    return getter


def discover_us_sources(
    jurisdiction: str,
    *,
    http_get: HttpGet | None = None,
    assignment_key: str | None = None,
    durable_store: Any = None,
    approved: bool = False,
    budget: DiscoveryBudget | None = None,
    env: Any = None,
    record_types: tuple[str, ...] = ("parcels", "appraisal"),
) -> dict[str, Any]:
    """Find, qualify, rank and (only when fully qualified) select US sources.

    Returns the retained selection in a compatibility shape. Catalog
    failure is reported honestly (empty ranking plus blockers), never
    papered over with invented sources.
    """
    from app.agents.gabriel.sources.discovery import select_for_mission

    name = str(jurisdiction or "").strip()
    if not name:
        raise ValueError("jurisdiction is required")
    env = os.environ if env is None else env
    mission = SourceMission(
        jurisdiction=name, record_types=tuple(record_types), country="US",
        approved=approved, budget=budget or DiscoveryBudget())
    catalog_urls: list[dict[str, str]] = [
        {"provider": provider, "url": url, "kind": kind}
        for provider, url, kind in _CATALOGS
    ]
    ckan_url = str(env.get("GABRIEL_CKAN_CATALOG_URL", "") or "").strip()
    if ckan_url:
        catalog_urls.append({"provider": "ckan_catalog", "url": ckan_url, "kind": "ckan"})
    try:
        decision = select_for_mission(
            mission, http_get=_wrap_http_get(http_get),
            assignment_key=assignment_key, durable_store=durable_store,
            env=env, catalog_urls=catalog_urls,
            supported_adapters={"generic_socrata", "generic_arcgis", "generic_ckan"})
    except SourceSelectionError as exc:
        # No approval / zero budget / all-catalog failure: no candidate
        # was selected and (for approval/budget) nothing transmitted.
        return {
            "jurisdiction": name, "country": "US", "ranked": [],
            "finding_policy": str(env.get("GABRIEL_FINDING_POLICY", "") or "").strip().lower()
            or "catalog_first",
            "selected_adapter": None, "selected_source": None,
            "selected_resource": None, "resource_digest": None,
            "status": "blocked",
            "authority_basis": (
                "none: catalog discovery never enrolls a publisher; "
                "registry enrollment remains a separate curated decision"),
            "blockers": [str(exc)],
            "provenance": "Socrata catalog, ArcGIS Hub, configured CKAN (free, keyless)",
        }
    ranked = [
        {
            "name": r.get("name"), "publisher_domain": r.get("publisher_domain"),
            "platform": (r.get("resource") or {}).get("platform") or r.get("provider"),
            "status": r.get("status"), "p1_eligible": r.get("p1_eligible"),
            "score": r.get("score"),
            "reasons": list(r.get("reasons") or []) + list(r.get("executable_blockers") or []),
            "adapter": r.get("adapter"),
            "resource_url": (r.get("resource") or {}).get("resource_url"),
            "dataset_id": (r.get("resource") or {}).get("dataset_id"),
        }
        for r in decision.get("ranked") or []
    ]
    blockers = list(decision.get("blockers") or [])
    if not ranked:
        blockers.append("no candidate sources found in any national catalog")
    return {
        "jurisdiction": name,
        "country": "US",
        "finding_policy": str(env.get("GABRIEL_FINDING_POLICY", "") or "").strip().lower()
        or "catalog_first",
        "ranked": ranked,
        "selected_adapter": decision.get("selected_adapter"),
        "selected_source": decision.get("selected_candidate"),
        "selected_resource": decision.get("selected_resource"),
        "resource_digest": decision.get("resource_digest"),
        "selection_digest": decision.get("digest"),
        "selection": decision,
        "status": decision.get("status"),
        "authority_basis": (
            "none: catalog discovery never enrolls a publisher; "
            "registry enrollment remains a separate curated decision"),
        "blockers": blockers,
        "provenance": "Socrata catalog, ArcGIS Hub, configured CKAN (free, keyless)",
    }
