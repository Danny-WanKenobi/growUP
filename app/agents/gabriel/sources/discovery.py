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

import copy
import hashlib
import json
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


def select_for_mission(mission, *, http_get=None, supported_adapters=None,
                       persist: bool = False, run_id: str = "",
                       assignment_key: str | None = None, durable_store=None,
                       brave_execute=None, reserve=None, env=None,
                       catalog_urls=None) -> dict[str, Any]:
    """Find, qualify, rank, and select for any jurisdiction (not only the 12 pilot counties).

    Without ``assignment_key`` this is the legacy in-process path.
    With a stable ``assignment_key`` it is the durable path: an
    exclusive claim (and, for Brave, the shared monthly reservation)
    is committed before any provider transmission, bounded response
    evidence and the decision are persisted in the existing workflow
    store, and a replay with the same key returns the retained
    decision with zero provider/DNS calls -- even after the process
    cache is cleared. A claimed attempt with no retained decision is
    uncertain and is never auto-retransmitted; the same key with a
    different mission/limits refuses.
    """
    from app.agents.gabriel.sources.source_finder import find_candidates
    from app.agents.gabriel.sources.source_selection import (
        SourceSelectionError, UncertainFindingError, WorkflowSelectionStore,
        mission_fingerprint, mission_payload, persist_selection, select_sources,
    )

    mission.validate()
    if not mission.approved:
        raise SourceSelectionError("source finding requires explicit approval")
    if assignment_key is None and durable_store is None:
        if http_get is None or brave_execute is not None or mission.budget.max_paid_requests:
            raise SourceSelectionError("real source finding requires a stable assignment_key")
        decision = select_sources(
            mission,
            find_candidates(mission, http_get=http_get, brave_execute=brave_execute,
                            reserve=reserve, env=env, catalog_urls=catalog_urls),
            supported_adapters=supported_adapters)
        if persist:
            persist_selection(decision, run_id=run_id or f"source-selection:{decision['digest'][:16]}")
        return decision
    if not str(assignment_key or "").strip():
        raise SourceSelectionError(
            "durable source finding requires a stable assignment_key")
    store = durable_store if durable_store is not None else WorkflowSelectionStore()
    fingerprint = mission_fingerprint(mission)
    claim = store.claim(assignment_key, fingerprint, mission_payload(mission))
    status = claim.get("status")
    if status == "completed":
        import copy as _copy
        from app.agents.gabriel.sources.source_selection import _RETAINED, _digest
        decision = _copy.deepcopy(claim["decision"])
        if decision.get("mission_fingerprint") != fingerprint \
                or decision.get("run_id") != claim.get("run_id") \
                or _digest(decision) != decision.get("digest"):
            raise SourceSelectionError(
                "retained source-selection decision does not verify against its run/digest/mission identity")
        _RETAINED[decision["digest"]] = _copy.deepcopy(decision)
        return decision
    if status != "winner":
        raise UncertainFindingError(
            "source finding for this assignment_key was already claimed and its "
            "outcome is uncertain; it will not be auto-retransmitted")
    run_identity = str(claim.get("run_id") or "")
    evidence_sink: list = []
    found = find_candidates(mission, http_get=http_get, brave_execute=brave_execute,
                            reserve=reserve, env=env, evidence_sink=evidence_sink,
                            catalog_urls=catalog_urls)
    decision = select_sources(mission, found, supported_adapters=supported_adapters)
    import hashlib as _hashlib
    import json as _json
    evidence = {
        "assignment_key": str(assignment_key), "run_id": run_identity,
        "mission_fingerprint": fingerprint, "mission": mission_payload(mission),
        "attempts": evidence_sink[:10],
        "candidates": [
            {"name": f.candidate.name, "publisher_domain": f.candidate.publisher_domain,
             "tier": f.tier, "provider": f.provider, "adapter": f.adapter,
             "evidence_ref": f.evidence_ref, "resource": dict(f.resource or {})}
            for f in found[: mission.budget.max_results]],
    }
    evidence_digest = _hashlib.sha256(
        _json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    from app.agents.gabriel.sources.source_selection import _RETAINED, _digest
    _RETAINED.pop(decision["digest"], None)
    decision.update({"assignment_key": str(assignment_key), "run_id": run_identity,
                     "mission_fingerprint": fingerprint,
                     "evidence_digest": evidence_digest})
    decision["digest"] = _digest(decision)
    store.complete(assignment_key, decision=decision, evidence=evidence)
    _RETAINED[decision["digest"]] = __import__("copy").deepcopy(decision)
    if persist:
        persist_selection(decision, run_id=run_id or f"source-selection:{decision['digest'][:16]}")
    return decision


def discover_sources_anywhere(
    jurisdiction: str, category: str, *, http_get=None, assignment_key=None,
    durable_store=None, approved: bool = False, budget=None, env=None,
    auto_enroll: bool = False, enrollment_store=None,
    auto_convert: bool = False,
) -> dict[str, Any]:
    """Nationwide path: any US jurisdiction, zero preliminary research.

    Thin delegation to the shared durable selection path over the
    national catalogs. The DFW pilot path above stays curated.
    Default (unapproved / zero budget) cannot network.

    With ``auto_enroll`` (owner-authorized autonomous enrollment),
    a selected generic resource is additionally verified directly
    against its publisher's own domain and Gabriel records his own
    enrollment decision (enrolled / needs_more_evidence) in the
    durable enrollment store. Selection executability never depends
    on enrollment; enrollment governs fact-level authority only.
    """
    if category not in CATEGORIES:
        raise ValueError(f"unknown pilot category: {category!r}")
    from app.agents.gabriel.sources.us_discovery import discover_us_sources

    result = discover_us_sources(
        jurisdiction, http_get=http_get, assignment_key=assignment_key,
        durable_store=durable_store, approved=approved, budget=budget, env=env)
    result["category"] = category
    _env = env if env is not None else __import__("os").environ
    _directory_urls = tuple(
        u.strip() for u in str(_env.get("GABRIEL_DIRECTORY_URLS", "") or "").split(",")
        if u.strip())
    if auto_enroll and result.get("status") == "selected":
        selection = result.get("selection") or {}
        resource = selection.get("selected_resource") or result.get("selected_resource")
        if isinstance(resource, dict) and resource:
            from app.agents.gabriel.sources.autonomous_enrollment import (
                EnrollmentError, run_autonomous_enrollment,
            )
            try:
                result["enrollment"] = run_autonomous_enrollment(
                    resource, jurisdiction=jurisdiction, store=enrollment_store,
                    directory_urls=_directory_urls)
            except EnrollmentError as exc:
                result["enrollment"] = {
                    "status": "needs_more_evidence",
                    "missing": [str(exc)],
                }
    elif auto_enroll and result.get("status") == "needs_review":
        # Catalog claims were insufficient: Gabriel asks the
        # publisher directly (bounded, at most three candidates,
        # two requests each). Only an enrolled publisher-direct
        # verification may turn this into a durable selection.
        from app.agents.gabriel.sources.autonomous_enrollment import (
            EnrollmentError, verify_and_select,
        )
        attempts: list[dict[str, Any]] = []
        # Jurisdiction-specific tokens only: generic words like
        # "county" match half the catalog and bury the local source.
        _tokens = [t for t in jurisdiction.lower().replace(",", " ").split()
                   if len(t) > 2 and t not in (
                       "county", "city", "parish", "borough", "state")]
        def _attempt_key(indexed):
            index, entry = indexed
            name = str(entry.get("name") or "").lower()
            domain = str(entry.get("publisher_domain") or "").lower()
            local = any(t in name or t in domain for t in _tokens)
            prop = any(t in name for t in (
                "parcel", "property", "properties", "appraisal",
                "building", "assessment", "land"))
            return (0 if local else 1, 0 if prop else 1, index)

        _ranked = [entry for _i, entry in sorted(
            enumerate(result.get("ranked") or []), key=_attempt_key)]
        for entry in _ranked[:8]:
            if entry.get("adapter") not in ("generic_socrata", "generic_arcgis") \
                    or not entry.get("resource_url"):
                continue
            try:
                verified = verify_and_select(
                    entry, jurisdiction=jurisdiction,
                    assignment_key=f"{assignment_key or jurisdiction}:verified:"
                                   f"{entry.get('publisher_domain')}:"
                                   f"{str(entry.get('resource_url')).rsplit('/', 1)[-1]}",
                    directory_urls=_directory_urls,
                )
            except EnrollmentError as exc:
                attempts.append({"candidate": entry.get("name"),
                                 "status": "needs_more_evidence",
                                 "missing": [str(exc)]})
                continue
            attempts.append({"candidate": entry.get("name"),
                             "enrollment_status":
                                 (verified.get("enrollment") or {}).get("status"),
                             "missing": (verified.get("enrollment") or {}).get("missing")})
            if verified.get("selection") is not None:
                selection = verified["selection"]
                result.update({
                    "status": "selected",
                    "selected_adapter": selection.get("selected_adapter"),
                    "selected_source": selection.get("selected_candidate"),
                    "selected_resource": selection.get("selected_resource"),
                    "resource_digest": selection.get("resource_digest"),
                    "selection_digest": selection.get("digest"),
                    "selection": selection,
                    "enrollment": verified.get("enrollment"),
                })
                break
        result["autonomous_attempts"] = attempts
    if auto_convert and result.get("status") != "selected":
        # Lead-to-source conversion: search leads (e.g. SearXNG
        # results) are resolved to concrete dataset resources and
        # run through the same publisher-direct verification. Only
        # a fully verified resource becomes a durable selection.
        from app.agents.gabriel.sources.lead_conversion import convert_leads

        converted = convert_leads(
            result.get("ranked") or [], jurisdiction=jurisdiction,
            assignment_key=str(assignment_key or jurisdiction),
            directory_urls=_directory_urls)
        result["lead_attempts"] = converted["attempts"]
        if converted.get("selection") is not None:
            selection = converted["selection"]
            result.update({
                "status": "selected",
                "selected_adapter": selection.get("selected_adapter"),
                "selected_source": selection.get("selected_candidate"),
                "selected_resource": selection.get("selected_resource"),
                "resource_digest": selection.get("resource_digest"),
                "selection_digest": selection.get("digest"),
                "selection": selection,
                "enrollment": converted.get("enrollment"),
            })
    return result


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
