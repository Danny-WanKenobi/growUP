"""Universal source-qualification policy for Gabriel (worldwide).

Separates the *universal qualification policy* (this module) from
*regional catalogs and adapters* (e.g. the Dallas profile in
``config/dallas_official_sources.example.json`` and the DCAD
adapters). Dallas is the first proven regional implementation, not
the global algorithm: nothing here names a Dallas source.

A candidate is evaluated on verifiable attributes only — publisher
identity and domain, jurisdiction, record types, access method,
cost, authentication, robots restrictions, licensing, format,
coverage, and freshness. Qualification never grants authority:
enrolling a source in the shared verification registry (with a
narrow, fact-level scope) remains a separate curated decision, and
a source authoritative for parcel identity is not thereby
authoritative for vacancy, availability, or suitability.

Rankings are dynamic: within qualified sources, ordering depends on
the mission's needed record types, access openness, machine-
readable formats, and freshness — never on a static worldwide list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "SourceCandidate",
    "Qualification",
    "qualify_source",
    "rank_sources",
]

_MACHINE_READABLE = {"csv", "json", "geojson", "shapefile", "api", "xml"}
_ACCESS_SCORE = {"bulk_download": 30, "api": 25, "portal_download": 15,
                 "portal_search": 5}


@dataclass(frozen=True)
class SourceCandidate:
    """One candidate source, described by verifiable attributes."""

    name: str
    publisher_name: str
    publisher_domain: str
    claimed_official: bool
    jurisdiction: str
    country: str
    record_types: tuple[str, ...] = ()
    access_method: str = "portal_search"  # bulk_download|api|portal_*|scrape
    free: bool = True
    requires_auth: bool = False
    robots_restricted: bool = False
    license_clear: bool = False
    formats: tuple[str, ...] = ()
    property_level: bool = True
    update_age_days: int | None = None
    provenance_url: str = ""


@dataclass(frozen=True)
class Qualification:
    """Deterministic qualification outcome (never an authority grant)."""

    status: str  # qualified | needs_review | rejected
    p1_eligible: bool
    score: int
    reasons: list[str] = field(default_factory=list)
    authority_candidates: tuple[str, ...] = ()


def _is_government_domain(domain: str) -> bool:
    host = domain.strip().lower().split("/")[0]
    return host.endswith(".gov") or ".gov." in host or host == "gov"


def qualify_source(candidate: SourceCandidate) -> Qualification:
    """Evaluate one candidate against the universal policy."""
    reasons: list[str] = []
    rejected = False
    if not candidate.free:
        reasons.append("requires payment; paid acquisition needs separate approval")
        rejected = True
    if candidate.requires_auth:
        reasons.append("requires authentication; no credentialed access is authorized")
        rejected = True
    if candidate.robots_restricted:
        reasons.append("robots/access restrictions prohibit the intended access")
        rejected = True
    if candidate.access_method == "scrape":
        reasons.append("page scraping is not an approved access method")
        rejected = True
    if not candidate.property_level:
        reasons.append("not property-level records (aggregate or index only)")
        rejected = True
    formats = {fmt.lower() for fmt in candidate.formats}
    if not formats & _MACHINE_READABLE:
        reasons.append("no machine-readable format (unsupported format)")
        rejected = True
    official_consistent = candidate.claimed_official and _is_government_domain(
        candidate.publisher_domain
    )
    publisher_unverified = candidate.claimed_official and not official_consistent
    if publisher_unverified:
        # Real authorities (e.g. Texas appraisal districts) often
        # publish on .org domains: the claim is neither accepted nor
        # rejected on TLD alone — publisher identity must be verified
        # with evidence before any P1 authority candidacy.
        reasons.append(
            "official publisher identity not established by domain; "
            "requires verifiable agency evidence before P1 candidacy"
        )
    if rejected:
        return Qualification("rejected", False, 0, reasons, ())

    score = _ACCESS_SCORE.get(candidate.access_method, 0)
    score += 10 * len(formats & _MACHINE_READABLE)
    if candidate.update_age_days is None:
        reasons.append("freshness unknown; update cadence must be verified")
    elif candidate.update_age_days <= 400:
        score += 20
    elif candidate.update_age_days <= 800:
        score += 10
        reasons.append("data is aging; verify update cadence before reliance")
    else:
        reasons.append("data appears stale; treat as historical only")
    if not candidate.license_clear:
        reasons.append("licensing/terms not yet verified")
    if not candidate.jurisdiction:
        reasons.append("jurisdiction not established")

    p1_eligible = bool(official_consistent)
    if not p1_eligible:
        reasons.append(
            "not a verified official publisher; may enrich after review "
            "but cannot be a P1 authority candidate"
        )
    needs_review = bool(reasons) and (
        publisher_unverified
        or not candidate.license_clear
        or candidate.update_age_days is None
        or not candidate.jurisdiction
    )
    return Qualification(
        status="needs_review" if needs_review else "qualified",
        p1_eligible=p1_eligible,
        score=score,
        reasons=reasons,
        authority_candidates=candidate.record_types if p1_eligible else (),
    )


def rank_sources(
    candidates: list[SourceCandidate],
    *,
    needed_record_types: tuple[str, ...] = (),
) -> list[tuple[SourceCandidate, Qualification]]:
    """Rank dynamically: qualification, mission relevance, freshness.

    Rejected candidates sort last; nothing here is a static list —
    the same candidate ranks differently for different missions.
    """
    needed = {item.lower() for item in needed_record_types}
    scored: list[tuple[int, int, int, str, SourceCandidate, Qualification]] = []
    for candidate in candidates:
        result = qualify_source(candidate)
        status_rank = {"qualified": 0, "needs_review": 1, "rejected": 2}[
            result.status
        ]
        relevance = len(
            needed & {item.lower() for item in candidate.record_types}
        )
        scored.append(
            (status_rank, -relevance, -result.score, candidate.name,
             candidate, result)
        )
    scored.sort(key=lambda item: item[:4])
    return [(candidate, result) for *_head, candidate, result in scored]
