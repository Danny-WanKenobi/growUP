"""Jurisdiction source discovery for Gabriel (worldwide policy, DFW catalog).

Connects the universal qualification policy
(``app.agents.gabriel.sources.qualification``) to the actual pilot
workflow: for a requested county/category this module assembles the
candidate sources known for that jurisdiction, qualifies and ranks
them dynamically, and selects an existing compatible adapter only
when the shared verification registry has independently enrolled the
publisher. Qualification alone never grants authority, and no
candidate earns P1 candidacy from a domain suffix or an LLM judgment
— every non-``.gov`` appraisal district below stays ``needs_review``
until publisher identity, licensing, and freshness are verified with
evidence and a curator enrolls it (the Dallas DCAD path).

The Dallas catalog remains the first proven regional implementation
(``config/dallas_official_sources.example.json`` plus the enrolled
``www.dallascad.org`` registry entry and the ``dcad_bulk`` adapter);
it is reused, not treated as a universal dependency. Other counties
currently have no compatible adapter, which is recorded as a blocker
rather than papered over with scraping or paid data.

Candidate provenance: Dallas facts come from the research-verified
2026-11-02 catalog in this repository. The remaining appraisal-
district names/domains were corroborated against public Texas
appraisal-district directories on 2026-11-02; their access methods,
formats, licensing, and freshness are explicitly unverified, so the
policy keeps them provisional. Live web discovery (SearXNG, then
budget-controlled Brave) remains a fallback behind an approved
collection envelope and is not performed here.
"""

from __future__ import annotations

from typing import Any

from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER, display_name
from app.agents.gabriel.source_registry import PRODUCTION_REGISTRY, SourceClass
from app.agents.gabriel.sources.qualification import (
    SourceCandidate,
    rank_sources,
)

CATEGORIES = ("high_rise", "commercial_property", "warehouse_industrial")
NEEDED_RECORD_TYPES = ("parcels", "appraisal")

# county (canonical) -> (publisher name, domain, provenance note)
_DFW_APPRAISAL_DISTRICTS: dict[str, tuple[str, str, str]] = {
    "dallas county": (
        "Dallas Central Appraisal District", "www.dallascad.org",
        "Research-verified Dallas catalog (config/dallas_official_sources.example.json); enrolled in the shared verification registry for recorded facts only.",
    ),
    "tarrant county": (
        "Tarrant Appraisal District", "www.tad.org",
        "Publisher/domain corroborated in public appraisal-district directories 2026-11-02; access, licensing, and freshness unverified.",
    ),
    "collin county": (
        "Collin Central Appraisal District", "www.collincad.org",
        "Publisher/domain corroborated in public appraisal-district directories 2026-11-02; access, licensing, and freshness unverified.",
    ),
    "denton county": (
        "Denton Central Appraisal District", "www.dentoncad.org",
        "Publisher/domain corroborated in public appraisal-district directories 2026-11-02 (a .com variant also appears in directories); access, licensing, and freshness unverified.",
    ),
    "ellis county": (
        "Ellis Appraisal District", "www.elliscad.org",
        "Publisher/domain corroborated in public appraisal-district directories 2026-11-02; access, licensing, and freshness unverified.",
    ),
    "johnson county": (
        "Johnson County Appraisal District", "www.johnsoncad.com",
        "Publisher/domain corroborated in public appraisal-district directories 2026-11-02; access, licensing, and freshness unverified.",
    ),
    "kaufman county": (
        "Kaufman County Appraisal District", "www.kaufmancad.org",
        "Publisher/domain corroborated in public appraisal-district directories 2026-11-02; access, licensing, and freshness unverified.",
    ),
    "parker county": (
        "Parker County Appraisal District", "www.parkercad.org",
        "Publisher/domain corroborated in public appraisal-district directories 2026-11-02; access, licensing, and freshness unverified.",
    ),
    "rockwall county": (
        "Rockwall Central Appraisal District", "www.rockwallcad.com",
        "Publisher/domain corroborated by district letterhead and public directories 2026-11-02; access, licensing, and freshness unverified.",
    ),
    "hunt county": (
        "Hunt County Appraisal District", "www.huntcad.org",
        "Publisher/domain corroborated in public appraisal-district directories 2026-11-02; access, licensing, and freshness unverified.",
    ),
    "wise county": (
        "Wise County Appraisal District", "www.co.wise.tx.us",
        "Only the Wise County site was corroborated 2026-11-02; a district-specific domain, access, licensing, and freshness are unverified.",
    ),
    "navarro county": (
        "Navarro Central Appraisal District", "www.navarrocad.com",
        "Publisher/domain corroborated by district publications 2026-11-02; access, licensing, and freshness unverified.",
    ),
}

# The only county with both a registry-enrolled publisher and an
# existing compatible adapter. Enrollment — not this table — is the
# authority; the adapter name is re-checked against the live source
# registry at selection time.
_ADAPTER_BY_COUNTY = {"dallas county": "dcad_bulk"}


def _normalize_county(county: str) -> str:
    wanted = (county or "").strip().lower()
    if wanted in COUNTY_PRIORITY_ORDER:
        return wanted
    for canonical in COUNTY_PRIORITY_ORDER:
        if display_name(canonical).lower() == wanted:
            return canonical
    raise ValueError(f"unknown pilot county: {county!r}")


def _candidate_for(county: str) -> SourceCandidate:
    name, domain, _note = _DFW_APPRAISAL_DISTRICTS[county]
    if county == "dallas county":
        # Verified Dallas catalog facts; licensing still says
        # re-verify at ingestion, so license_clear stays False.
        return SourceCandidate(
            name=name, publisher_name=name, publisher_domain=domain,
            claimed_official=True, jurisdiction=display_name(county),
            country="US", record_types=NEEDED_RECORD_TYPES,
            access_method="bulk_download", free=True, requires_auth=False,
            robots_restricted=False, license_clear=False,
            formats=("csv", "shapefile"), property_level=True,
            update_age_days=None,
            provenance_url="https://www.dallascad.org/GISDataProducts.aspx",
        )
    return SourceCandidate(
        name=name, publisher_name=name, publisher_domain=domain,
        claimed_official=True, jurisdiction=display_name(county),
        country="US", record_types=NEEDED_RECORD_TYPES,
        access_method="portal_search", free=True, requires_auth=False,
        robots_restricted=False, license_clear=False,
        formats=("csv",), property_level=True, update_age_days=None,
        provenance_url=f"https://{domain}/",
    )


def _registry_enrolled(domain: str) -> bool:
    entry = PRODUCTION_REGISTRY.lookup(domain)
    return entry is not None and entry.source_class is SourceClass.AUTHORITATIVE


def discover_sources(county: str, category: str) -> dict[str, Any]:
    """Qualify, rank, and adapter-select the sources for one cell.

    Offline and deterministic: no network, no spending, no authority
    grant. ``selected_adapter`` is set only when a compatible adapter
    exists *and* the publisher is enrolled in the shared registry.
    """
    canonical = _normalize_county(county)
    if category not in CATEGORIES:
        raise ValueError(f"unknown pilot category: {category!r}")
    _name, domain, note = _DFW_APPRAISAL_DISTRICTS[canonical]
    ranked_pairs = rank_sources(
        [_candidate_for(canonical)], needed_record_types=NEEDED_RECORD_TYPES)
    adapter_name = _ADAPTER_BY_COUNTY.get(canonical)
    enrolled = _registry_enrolled(domain)
    selected: str | None = None
    authority_basis = "none: no registry-enrolled publisher for this county"
    blockers: list[str] = []
    if adapter_name is not None:
        # Importing the sources package registers every adapter.
        from app.agents.gabriel.sources.base import SOURCES

        if not enrolled:
            blockers.append(
                "publisher is not enrolled in the shared verification "
                "registry; adapter selection refused")
        elif adapter_name not in SOURCES:
            blockers.append(f"adapter {adapter_name!r} is not registered")
        else:
            selected = adapter_name
            authority_basis = (
                "shared verification registry enrollment for "
                f"{domain} (recorded facts only; never vacancy, "
                "availability, or suitability)")
    else:
        blockers.append(
            "no compatible adapter exists for this county yet; "
            "publisher verification and adapter work are required "
            "before any collection")
    ranked = [
        {
            "name": candidate.name,
            "publisher_domain": candidate.publisher_domain,
            "status": result.status,
            "p1_eligible": result.p1_eligible,
            "score": result.score,
            "reasons": list(result.reasons),
            "authority_candidates": list(result.authority_candidates),
            "adapter": selected if candidate.publisher_domain == domain else None,
        }
        for candidate, result in ranked_pairs
    ]
    if ranked and ranked[0]["status"] != "qualified":
        blockers.append(
            f"top source is {ranked[0]['status']}: "
            + "; ".join(ranked[0]["reasons"]))
    return {
        "county": canonical,
        "county_display": display_name(canonical),
        "category": category,
        "needed_record_types": list(NEEDED_RECORD_TYPES),
        "ranked": ranked,
        "selected_adapter": selected,
        "authority_basis": authority_basis,
        "provenance": note,
        "blockers": blockers,
    }


def qualified_adapter(county: str, category: str) -> str | None:
    """The adapter the workflow may use for a cell, or None."""
    return discover_sources(county, category)["selected_adapter"]


def pilot_source_plan() -> list[dict[str, Any]]:
    """The 36-cell source plan (12 counties x 3 canonical categories)."""
    return [
        discover_sources(county, category)
        for county in COUNTY_PRIORITY_ORDER
        for category in CATEGORIES
    ]
