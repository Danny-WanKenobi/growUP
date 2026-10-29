"""Evidence-driven county and property-category attribution (B5-2A, step 1).

Pure data contracts plus a deterministic, side-effect-free county
evidence evaluator. No network, database, or filesystem access.

Three independent concepts stay separate:

- Evidence status: what the claims establish (unresolved, observed,
  corroborated, contradictory, verified).
- Review status: whether human review was requested (not requested,
  required, pending). Step 1 only models the states; escalation
  belongs to a later step.
- Territory eligibility: whether a verified county lies inside the
  authorized operating territory (unknown, within, outside). A
  verified out-of-territory county stays verified.

The search-target county/category is carried as context only and is
never promoted into an established value.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.agents.gabriel.geography import COUNTY_PRIORITY_ORDER

_COUNTY_RE = re.compile(r"^\s*([A-Za-z][A-Za-z .'\-]*?)\s+[Cc]ounty\s*(?:,\s*[A-Za-z]{2}\s*)?$")
_WS_RE = re.compile(r"\s+")

_AUTHORITATIVE = "authoritative"

# Origins whose agreement may verify a category. The search assignment
# itself ("search") never counts: search intent is not property evidence.
# Manual intake assertions ("intake") are preserved as observations but
# never count either: one caller's assertion is not independent evidence.
_CATEGORY_VERIFY_ORIGINS = frozenset({"osm_tags", "listing_text", "provider"})

_KNOWN_CATEGORIES = frozenset(
    {
        "high_rise",
        "downtown_commercial",
        "warehouse_industrial",
        "other",
    }
)


class CountyEvidenceStatus(str, Enum):
    """What the collected county claims establish."""

    UNRESOLVED = "unresolved"
    OBSERVED = "observed"
    CORROBORATED = "corroborated"
    CONTRADICTORY = "contradictory"
    VERIFIED = "verified"


class AttributionReviewStatus(str, Enum):
    """Human-review state for an attribution question (modeled, not routed)."""

    NOT_REQUESTED = "not_requested"
    REQUIRED = "required"
    PENDING = "pending"


class TerritoryEligibility(str, Enum):
    """Where a verified county sits relative to the operating territory."""

    UNKNOWN = "unknown"
    WITHIN_TERRITORY = "within_territory"
    OUTSIDE_TERRITORY = "outside_territory"


class CategoryEvidenceStatus(str, Enum):
    """What the collected category observations establish."""

    UNKNOWN = "unknown"
    OBSERVED = "observed"
    VERIFIED = "verified"
    CONTRADICTORY = "contradictory"


class CountyClaim(BaseModel):
    """One preserved county claim with its provenance metadata."""

    original: str = ""
    normalized: str | None = None
    source_url: str = ""
    source_domain: str = ""
    excerpt: str = ""
    observed_at: str | None = None
    round_no: int = 0
    property_match: str = ""
    authority: str = "listing"
    syndication_id: str | None = None


class CountyAttribution(BaseModel):
    """Attribution state for one property's county question."""

    evidence_status: CountyEvidenceStatus = CountyEvidenceStatus.UNRESOLVED
    review_status: AttributionReviewStatus = AttributionReviewStatus.NOT_REQUESTED
    territory: TerritoryEligibility = TerritoryEligibility.UNKNOWN
    value: str | None = None
    claims: list[CountyClaim] = Field(default_factory=list)
    conflicting_values: list[str] = Field(default_factory=list)
    method: str = ""
    search_county: str = ""

    @model_validator(mode="after")
    def _check_consistent(self) -> CountyAttribution:
        verified = self.evidence_status is CountyEvidenceStatus.VERIFIED
        if verified:
            if self.value is None or normalize_county_claim(self.value) != self.value:
                raise ValueError("verified county attribution requires a canonical verified value")
            if self.territory is TerritoryEligibility.UNKNOWN:
                raise ValueError("verified county attribution requires a territory determination")
        else:
            if self.value is not None:
                raise ValueError("non-verified county attribution must not carry a verified value")
            if self.territory is not TerritoryEligibility.UNKNOWN:
                raise ValueError("territory must stay unknown without verified county attribution")
        if (
            self.evidence_status is CountyEvidenceStatus.CONTRADICTORY
            and len(self.conflicting_values) < 2
        ):
            raise ValueError("contradictory county attribution requires conflicting values")
        return self


class CategoryObservation(BaseModel):
    """One preserved category observation with its origin."""

    value: str = ""
    origin: str = ""
    source_url: str = ""


class CategoryAttribution(BaseModel):
    """Attribution state for one property's category question."""

    status: CategoryEvidenceStatus = CategoryEvidenceStatus.UNKNOWN
    search_category: str = ""
    observed: str | None = None
    observed_origins: list[str] = Field(default_factory=list)
    verified: str | None = None
    conflicting_values: list[str] = Field(default_factory=list)
    observations: list[CategoryObservation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_consistent(self) -> CategoryAttribution:
        verified = self.status is CategoryEvidenceStatus.VERIFIED
        if verified:
            if self.verified is None or normalize_category_value(self.verified) != self.verified:
                raise ValueError("verified category attribution requires a known verified value")
        elif self.verified is not None:
            raise ValueError("non-verified category attribution must not carry a verified value")
        if (
            self.status is CategoryEvidenceStatus.CONTRADICTORY
            and len(self.conflicting_values) < 2
        ):
            raise ValueError("contradictory category attribution requires conflicting values")
        return self


def normalize_county_claim(text: str) -> str | None:
    """Normalize a county claim to canonical form, or None if malformed.

    Canonical form is lowercase "<name> county". A bare municipality
    ("Dallas"), an empty string, or anything that does not name a
    county is malformed: the caller preserves the original and treats
    the claim as unusable. An optional trailing ", ST" state suffix is
    formatting, not county information, and is discarded.
    """
    if not isinstance(text, str):
        return None
    match = _COUNTY_RE.match(text)
    if match is None:
        return None
    name = _WS_RE.sub(" ", match.group(1)).strip().lower()
    if not name:
        return None
    return f"{name} county"


def normalize_category_value(text: str) -> str | None:
    """Normalize a category observation to a known value, or None."""
    if not isinstance(text, str):
        return None
    cleaned = _WS_RE.sub(" ", text).strip().lower().replace("-", "_").replace("/", "_").replace(" ", "_")
    cleaned = re.sub(r"_+", "_", cleaned)
    if cleaned in _KNOWN_CATEGORIES:
        return cleaned
    return None


def _independent_groups(claims: list[CountyClaim]) -> list[list[CountyClaim]]:
    """Partition claims into independent corroboration voices.

    Claims sharing a non-empty syndication id describe one underlying
    report and form one voice however many domains repeat them. Within
    the remaining claims, one domain is one voice. Deterministic.
    """
    syndicated: dict[str, list[CountyClaim]] = {}
    rest: list[CountyClaim] = []
    for claim in claims:
        if claim.syndication_id:
            syndicated.setdefault(claim.syndication_id, []).append(claim)
        else:
            rest.append(claim)
    groups = [sorted(group, key=lambda c: (c.source_domain, c.source_url, c.original)) for group in syndicated.values()]
    by_domain: dict[str, list[CountyClaim]] = {}
    for claim in rest:
        by_domain.setdefault(claim.source_domain, []).append(claim)
    groups.extend(
        sorted(group, key=lambda c: (c.source_url, c.original)) for group in by_domain.values()
    )
    groups.sort(key=lambda group: (group[0].source_domain, group[0].source_url, group[0].original))
    return groups


def evaluate_county_evidence(
    claims: list[CountyClaim] | list[dict],
    *,
    search_county: str = "",
    territory: tuple[str, ...] | frozenset[str] = COUNTY_PRIORITY_ORDER,
) -> CountyAttribution:
    """Evaluate supplied county claims deterministically (pure).

    Only the given claims are considered; input order never affects
    the outcome. Malformed claims are preserved with normalized None.
    The search-target county is recorded as context and can never
    become the established value.
    """
    parsed = [claim if isinstance(claim, CountyClaim) else CountyClaim.model_validate(claim) for claim in claims]
    ordered = sorted(parsed, key=lambda c: (c.original, c.source_domain, c.source_url, c.round_no))
    territory_set = frozenset(territory)
    base = CountyAttribution(claims=ordered, search_county=search_county)
    usable = [claim for claim in ordered if claim.normalized]
    if not usable:
        base.method = "no usable county evidence"
        return base
    distinct = sorted({claim.normalized for claim in usable if claim.normalized})
    authoritative = [
        claim
        for claim in usable
        if claim.authority == _AUTHORITATIVE and claim.property_match.strip()
    ]
    auth_values = sorted({claim.normalized for claim in authoritative if claim.normalized})
    if len(auth_values) > 1:
        base.evidence_status = CountyEvidenceStatus.CONTRADICTORY
        base.conflicting_values = auth_values
        base.method = "conflicting authoritative determinations preserved"
        return base
    if len(auth_values) == 1:
        others = sorted(value for value in distinct if value != auth_values[0])
        base.evidence_status = CountyEvidenceStatus.VERIFIED
        base.value = auth_values[0]
        base.conflicting_values = others
        base.method = "authoritative property-specific determination"
        base.territory = (
            TerritoryEligibility.WITHIN_TERRITORY
            if auth_values[0] in territory_set
            else TerritoryEligibility.OUTSIDE_TERRITORY
        )
        return base
    if len(distinct) > 1:
        base.evidence_status = CountyEvidenceStatus.CONTRADICTORY
        base.conflicting_values = distinct
        base.method = "conflicting county claims preserved"
        return base
    sole = [claim for claim in usable if claim.normalized == distinct[0]]
    if len(_independent_groups(sole)) >= 2:
        base.evidence_status = CountyEvidenceStatus.CORROBORATED
        base.method = "independent corroborating claims"
        return base
    base.evidence_status = CountyEvidenceStatus.OBSERVED
    base.method = "single uncorroborated claim"
    return base


def evaluate_category_evidence(
    observations: list[CategoryObservation] | list[dict],
    *,
    search_category: str = "",
) -> CategoryAttribution:
    """Evaluate supplied category observations deterministically (pure).

    The search-target category is recorded as context only. A verified
    category requires agreement between at least two distinct
    evidentiary origins; the search assignment itself never counts.
    """
    parsed = [
        observation if isinstance(observation, CategoryObservation) else CategoryObservation.model_validate(observation)
        for observation in observations
    ]
    ordered = sorted(parsed, key=lambda o: (o.value, o.origin, o.source_url))
    result = CategoryAttribution(search_category=search_category, observations=ordered)
    known = [(normalize_category_value(item.value), item.origin) for item in ordered]
    known = [(value, origin) for value, origin in known if value is not None]
    if not known:
        return result
    distinct = sorted({value for value, _ in known})
    if len(distinct) > 1:
        result.status = CategoryEvidenceStatus.CONTRADICTORY
        result.conflicting_values = distinct
        return result
    value = distinct[0]
    result.observed = value
    result.observed_origins = sorted({origin for _, origin in known})
    verify_origins = {origin for _, origin in known} & _CATEGORY_VERIFY_ORIGINS
    if len(verify_origins) >= 2:
        result.status = CategoryEvidenceStatus.VERIFIED
        result.verified = value
    else:
        result.status = CategoryEvidenceStatus.OBSERVED
    return result
