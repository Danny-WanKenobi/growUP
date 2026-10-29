"""Property value ranking, opportunity ranking, and quality control (MVP-004B-4).

Deterministic, explainable, evidence-driven prioritization over the
extractor's intelligence. No API calls, no database writes, no AI calls.

Pipeline: planner assignments -> Brave results -> extractor intelligence
-> quality-control gates -> two independent rankings:

- Value ranking answers "which properties represent the largest potential
  transactions?" by transparent ordering on attributed current asking
  prices. Unpriced candidates are listed as unpriced, never scored.
- Opportunity ranking answers "which properties look promising for indoor
  farm conversion research?" via weighted factors with per-factor
  explanations. Unknown evidence reduces completeness instead of scoring
  zero, and thin candidates are marked insufficient rather than ranked.

Quality control screens out only obviously invalid leads (demolished,
unsuitable structure, incompatible use, unidentifiable results) with
auditable rules. Everything uncertain stays eligible as RESEARCH_REQUIRED
with explicit next steps for Raphael, Onesimus, and Priscilla -- whose
conclusions Gabriel never fabricates.

Geography (ten-county expansion): the operating area is Dallas County
(primary territory) plus nine secondary counties. Properties reliably
outside all ten counties are DEFERRED from the active queue with
evidence retained -- never silently discarded. Unknown county is never
a disqualification.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field

from app.agents.gabriel.extractor import (
    MoneyKind,
    ResearchIntelligence,
    ResultIntelligence,
    TemporalStatus,
    assembled_identity,
    eligible_identity_props,
    group_by_property,
    identity_disputes,
)
from app.agents.gabriel.geography import DALLAS_COUNTY, SECONDARY_COUNTIES

RANKING_VERSION = "004B-5.0"

# Initial product heuristics (not validated investment criteria).
WEIGHT_VALUE = 0.30
WEIGHT_DISTRESS = 0.25
WEIGHT_UNDERUSE = 0.20
WEIGHT_BUILDING = 0.15
WEIGHT_EVIDENCE = 0.10

# Downtown is a tiebreak-scale secondary bonus, municipality-neutral.
DOWNTOWN_BONUS = 0.05

# Candidates below this evidence completeness are listed as insufficient
# rather than ranked (avoids incomparable scores).
COMPLETENESS_FLOOR = 0.50

# Value-factor magnitude tiers (documented heuristics, not valuations).
HIGH_VALUE_THRESHOLD = Decimal("10000000")
MID_VALUE_THRESHOLD = Decimal("1000000")

_SEVERE_DISTRESS = {"bankruptcy", "foreclosure", "receivership"}


class CountyEligibility(str, Enum):
    """Geographic research priority (ten-county model).

    Compatibility note: ``inside_county``/``outside_county`` from
    MVP-004B-4 were replaced by ``primary_area``/``secondary_area``/
    ``outside_operating_area``. ``county_unknown`` is unchanged.
    """

    PRIMARY_AREA = "primary_area"
    SECONDARY_AREA = "secondary_area"
    COUNTY_UNKNOWN = "county_unknown"
    OUTSIDE_OPERATING_AREA = "outside_operating_area"


class QCStatus(str, Enum):
    PASS = "pass"
    RESEARCH_REQUIRED = "research_required"
    EXCLUDE = "exclude"
    DEFERRED = "deferred"  # outside the operating area; evidence retained


class FactorEvidence(BaseModel):
    excerpt: str
    url: str
    date: str | None = None


class FactorAssessment(BaseModel):
    factor: str
    score: float | None = None  # None means unevaluated (unknown evidence)
    weight: float
    evidence: list[FactorEvidence] = []
    unknowns: list[str] = []
    historical: bool = False
    property_specific: bool = True
    explanation: str = ""


class OpportunityAssessment(BaseModel):
    score: float | None = None
    completeness: float = 0.0
    sufficient: bool = False
    downtown_bonus: float = 0.0
    factors: list[FactorAssessment] = []


class ExclusionRecord(BaseModel):
    rule: str
    evidence: str
    source_url: str
    why_sufficient: str
    identity: str
    date: str | None = None


class SignalRef(BaseModel):
    signal: str
    evidence: str
    url: str
    date: str | None = None


class ValueDetail(BaseModel):
    amount: str
    evidence: str
    url: str
    date: str | None = None
    note: str = "asking price, not a market valuation"


class RankedCandidate(BaseModel):
    key: str
    name: str | None = None
    city: str | None = None
    state: str | None = None
    identity_disputes: dict[str, list[str]] = Field(default_factory=dict)
    county: CountyEligibility = CountyEligibility.COUNTY_UNKNOWN
    county_evidence: str = ""
    qc_status: QCStatus = QCStatus.RESEARCH_REQUIRED
    exclusions: list[ExclusionRecord] = []
    deferrals: list[ExclusionRecord] = []
    unresolved: list[str] = []
    next_steps: list[str] = []
    value_rank: int | None = None
    value_detail: ValueDetail | None = None
    opportunity: OpportunityAssessment = OpportunityAssessment()
    downtown: bool = False
    distress: list[SignalRef] = []
    underuse: list[SignalRef] = []
    sources: list[str] = []
    source_roles: dict[str, list[str]] = Field(default_factory=dict)


class ScreenedLead(BaseModel):
    result_url: str
    title: str
    rule: str
    reason: str


class PropertyRanking(BaseModel):
    candidates: list[RankedCandidate] = []
    value_order: list[str] = []  # candidate keys by asking price, descending
    unpriced: list[str] = []  # candidate keys without an attributed asking price
    opportunity_order: list[str] = []  # sufficiently evidenced keys by score
    insufficient_data: list[str] = []  # candidate keys below the completeness floor
    screened_leads: list[ScreenedLead] = []
    ranking_version: str = RANKING_VERSION


# ---------------------------------------------------------------------------
# Attribution helpers (conservative: single-property results only)
# ---------------------------------------------------------------------------


def _is_historical(item: ResultIntelligence) -> bool:
    return item.temporal.status is TemporalStatus.HISTORICAL


def _item_date(item: ResultIntelligence) -> str | None:
    return item.temporal.published_at or item.retrieved_at


def _single_property(item: ResultIntelligence) -> bool:
    return len(item.properties) == 1


# ---------------------------------------------------------------------------
# Candidate assembly from conservative identity groups
# ---------------------------------------------------------------------------


class _CandidateEvidence:
    """Attributed observations collected for one identity group."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.members: list[int] = []
        self.asking: list[tuple[Decimal, ResultIntelligence, str]] = []
        self.scale_money: list[tuple[str, ResultIntelligence, str]] = []
        self.occupancy: list[tuple[ResultIntelligence, object]] = []
        self.distress: dict[str, tuple[ResultIntelligence, str]] = {}
        self.underuse: dict[str, tuple[ResultIntelligence, str]] = {}
        self.distress_refs: list[tuple[str, ResultIntelligence, str]] = []
        self.underuse_refs: list[tuple[str, ResultIntelligence, str]] = []
        self.structure: dict[str, tuple[ResultIntelligence, str]] = {}
        self.counties: list[tuple[str, ResultIntelligence]] = []
        self.downtown_hits: list[ResultIntelligence] = []
        self.unresolved: list[str] = []
        self.contradictions: list[str] = []
        self.contradiction_urls: set[str] = set()
        self.asking_contested: bool = False
        self.domains: set[str] = set()
        self.has_current_evidence = False


def _collect_candidate(
    key: str, members: list[int], items: list[ResultIntelligence]
) -> _CandidateEvidence:
    evidence = _CandidateEvidence(key)
    evidence.members = list(members)
    asking_amounts: list[tuple[Decimal, ResultIntelligence, str]] = []
    for index in members:
        item = items[index]
        single = _single_property(item)
        # Corroboration credit requires an attributable fact: membership
        # alone (multi-property, identity-only, or claimless results)
        # never counts as independent claim support.
        contributed = False
        if not single and (
            item.money or item.occupancy or item.distress or item.underuse
        ):
            evidence.unresolved.append(
                f"result {index + 1} mentions {len(item.properties)} properties; "
                "its observations cannot be safely attributed"
            )
        for mention in item.money:
            excerpt = mention.evidence
            if not single:
                continue  # unresolved (recorded above)
            if mention.amount_high is not None:
                evidence.unresolved.append(
                    f"price range {mention.amount} to {mention.amount_high} reported; "
                    "a range cannot headline a single asking price"
                )
                continue  # unresolved-only: no corroboration credit
            if mention.kind is MoneyKind.ASKING_PRICE_CURRENT and not _is_historical(item):
                asking_amounts.append((mention.amount, item, excerpt))
                contributed = True
            elif mention.kind in (
                MoneyKind.ASKING_PRICE_HISTORICAL,
                MoneyKind.SALE_PRICE_COMPLETED,
                MoneyKind.TRANSACTION_PROPOSED,
                MoneyKind.MARKET_VALUE_ESTIMATED,
            ):
                evidence.scale_money.append((mention.kind.value, item, excerpt))
                contributed = True
            else:
                evidence.unresolved.append(
                    f"unclassified monetary amount {mention.amount} cannot support valuation"
                )
        for observation in item.occupancy:
            if not single:
                continue
            if observation.scope.value == "whole_building":
                evidence.occupancy.append((item, observation))
                contributed = True
            else:
                evidence.unresolved.append(
                    "partial-scope occupancy figure cannot represent the whole building"
                )
        for signal in item.distress:
            if single:
                evidence.distress_refs.append((signal.signal, item, signal.evidence))
                if signal.signal not in evidence.distress:
                    evidence.distress[signal.signal] = (item, signal.evidence)
                contributed = True
        for signal in item.underuse:
            if single:
                evidence.underuse_refs.append((signal.signal, item, signal.evidence))
                if signal.signal not in evidence.underuse:
                    evidence.underuse[signal.signal] = (item, signal.evidence)
                contributed = True
        for signal in item.structure:
            if single and signal.signal not in evidence.structure:
                evidence.structure[signal.signal] = (item, signal.evidence)
                contributed = True
        for county in item.county_mentions:
            if single:
                evidence.counties.append((county, item))
                contributed = True
            else:
                evidence.unresolved.append(
                    f"county mention '{county}' cannot be attributed among {len(item.properties)} properties"
                )
        if item.mentions_downtown and single:
            evidence.downtown_hits.append(item)
            contributed = True
        if contributed:
            evidence.domains.add(item.domain or item.result_url)
            if item.temporal.status is TemporalStatus.CURRENT:
                evidence.has_current_evidence = True
    # Multiple distinct current askings conflict: no endpoint headlines;
    # the contradiction forces reconciliation rather than silent choice.
    distinct = sorted({amount for amount, _, _ in asking_amounts})
    if len(distinct) > 1:
        low, high = distinct[0], distinct[-1]
        if high > low * Decimal("1.05"):
            evidence.contradictions.append(
                f"conflicting asking prices {low} vs {high}; Raphael must reconcile"
            )
            evidence.contradiction_urls.update(item.result_url for _, item, _ in asking_amounts)
            evidence.asking_contested = True
    evidence.asking = asking_amounts
    # Occupancy/vacancy conflict: high occupancy plus vacancy evidence.
    occupancy_pcts = [
        obs.occupancy_pct for _, obs in evidence.occupancy if obs.occupancy_pct is not None
    ]
    vacancy_pcts = [
        obs.vacancy_pct for _, obs in evidence.occupancy if obs.vacancy_pct is not None
    ]
    if occupancy_pcts and max(occupancy_pcts) >= 80 and (
        "high_vacancy" in evidence.underuse or any(pct >= 30 for pct in vacancy_pcts)
    ):
        evidence.contradictions.append(
            "high reported occupancy conflicts with vacancy evidence; Raphael must reconcile"
        )
        evidence.contradiction_urls.update(
            item.result_url for item, obs in evidence.occupancy
            if obs.occupancy_pct is not None or (obs.vacancy_pct or 0) >= 30
        )
        if "high_vacancy" in evidence.underuse:
            item, _ = evidence.underuse["high_vacancy"]
            evidence.contradiction_urls.add(item.result_url)
    return evidence


# ---------------------------------------------------------------------------
# Geographic eligibility (explicit county evidence only)
# ---------------------------------------------------------------------------


def _assess_county(evidence: _CandidateEvidence) -> tuple[CountyEligibility, str]:
    distinct = sorted({name.lower() for name, _ in evidence.counties})
    if not distinct:
        return CountyEligibility.COUNTY_UNKNOWN, "no county evidence; municipality alone never assigns county"
    if distinct == [DALLAS_COUNTY]:
        return CountyEligibility.PRIMARY_AREA, "explicit 'Dallas County' evidence (primary territory)"
    if len(distinct) == 1:
        only = distinct[0]
        if only in SECONDARY_COUNTIES:
            return CountyEligibility.SECONDARY_AREA, f"explicit '{only}' evidence (secondary territory)"
        return CountyEligibility.OUTSIDE_OPERATING_AREA, f"explicit '{only}' evidence (outside operating area)"
    evidence.contradictions.append(
        f"conflicting county evidence ({', '.join(distinct)}); Raphael must determine which county holds the property"
    )
    evidence.contradiction_urls.update(item.result_url for _, item in evidence.counties)
    return (
        CountyEligibility.COUNTY_UNKNOWN,
        f"multiple counties mentioned ({', '.join(distinct)}); attribution ambiguous",
    )


def _geo_priority(county: CountyEligibility) -> int:
    """Deterministic ordering tiebreak only; never a score penalty."""
    if county is CountyEligibility.PRIMARY_AREA:
        return 2
    if county is CountyEligibility.SECONDARY_AREA:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Quality control
# ---------------------------------------------------------------------------


def _identity_label(items: list[ResultIntelligence], members: list[int], group_key: str) -> str:
    for index in members:
        for prop in eligible_identity_props(items[index], group_key):
            bits = [prop.name, prop.street_address, prop.city]
            label = ", ".join(bit for bit in bits if bit)
            if label:
                return label
    return "unidentified candidate"


def _evaluate_qc(
    candidate: RankedCandidate, evidence: _CandidateEvidence, items: list[ResultIntelligence]
) -> None:
    identity = _identity_label(items, evidence.members, evidence.key)
    if "demolished" in evidence.structure:
        item, excerpt = evidence.structure["demolished"]
        candidate.exclusions.append(
            ExclusionRecord(
                rule="demolished_structure",
                evidence=excerpt,
                source_url=item.result_url,
                why_sufficient="explicit demolition evidence; a demolished building offers no convertible structure",
                identity=identity,
                date=_item_date(item),
            )
        )
    if "land_vacant" in evidence.structure and not any(
        prop.name for index in evidence.members for prop in items[index].properties
    ):
        item, excerpt = evidence.structure["land_vacant"]
        candidate.exclusions.append(
            ExclusionRecord(
                rule="unsuitable_structure",
                evidence=excerpt,
                source_url=item.result_url,
                why_sufficient="vacant land or lot with no existing convertible commercial structure",
                identity=identity,
                date=_item_date(item),
            )
        )
    if "incompatible_use" in evidence.structure:
        item, excerpt = evidence.structure["incompatible_use"]
        candidate.exclusions.append(
            ExclusionRecord(
                rule="incompatible_use",
                evidence=excerpt,
                source_url=item.result_url,
                why_sufficient="explicit single-family residential use is fundamentally incompatible with commercial conversion",
                identity=identity,
                date=_item_date(item),
            )
        )
    if candidate.exclusions:
        candidate.qc_status = QCStatus.EXCLUDE
        return
    if candidate.county is CountyEligibility.OUTSIDE_OPERATING_AREA:
        county, item = evidence.counties[0]
        candidate.deferrals.append(
            ExclusionRecord(
                rule="outside_operating_area",
                evidence=f"explicit county evidence '{county}'",
                source_url=item.result_url,
                why_sufficient="reliable county evidence places the property outside the ten-county operating area",
                identity=identity,
                date=_item_date(item),
            )
        )
        candidate.qc_status = QCStatus.DEFERRED
        return
    candidate.unresolved.extend(evidence.unresolved)
    candidate.unresolved.extend(f"contradiction: {text}" for text in evidence.contradictions)
    if "proposed" in evidence.structure:
        candidate.unresolved.append("structure may be merely proposed; Raphael must confirm it exists")
    needs_research = bool(candidate.unresolved)
    if candidate.county is CountyEligibility.COUNTY_UNKNOWN:
        needs_research = True
    thin = not (evidence.asking or evidence.occupancy or evidence.distress or evidence.underuse)
    if thin:
        needs_research = True
    candidate.qc_status = QCStatus.RESEARCH_REQUIRED if needs_research else QCStatus.PASS


def _next_steps(candidate: RankedCandidate, evidence: _CandidateEvidence) -> list[str]:
    if candidate.qc_status is QCStatus.DEFERRED:
        return [
            "Gabriel: retain as a deferred out-of-area lead; advance only if the operating area expands"
        ]
    steps: list[str] = []
    if candidate.county is CountyEligibility.COUNTY_UNKNOWN:
        steps.append("Raphael: verify county location within the ten-county operating area via appraisal district or parcel records")
    if not evidence.asking:
        steps.append("Gabriel: seek a current advertised asking price from listing sources")
    if not evidence.occupancy:
        steps.append("Raphael: verify current occupancy and vacancy from building sources")
    if evidence.unresolved or evidence.contradictions:
        steps.append("Raphael: attribute multi-property facts and reconcile conflicting reports")
    if "proposed" in evidence.structure:
        steps.append("Raphael: confirm the structure is constructed, not merely proposed")
    steps.append("Onesimus: assess Kratky conversion suitability once identity is confirmed")
    steps.append("Priscilla: evaluate acquisition economics once pricing evidence exists")
    return steps


# ---------------------------------------------------------------------------
# Opportunity scoring (weighted, explainable, unknown-aware)
# ---------------------------------------------------------------------------


def _ref(item: ResultIntelligence, excerpt: str) -> FactorEvidence:
    return FactorEvidence(excerpt=excerpt[:300], url=item.result_url, date=_item_date(item))


def _score_value(evidence: _CandidateEvidence) -> FactorAssessment:
    assessment = FactorAssessment(factor="value", weight=WEIGHT_VALUE)
    if evidence.asking_contested:
        for _, item, excerpt in evidence.asking:
            assessment.evidence.append(_ref(item, excerpt))
        assessment.unknowns.append(
            "asking price contested by conflicting reports; no authoritative value selected"
        )
        assessment.explanation = "asking price contested; factor excluded from scoring, not scored zero"
        return assessment
    if evidence.asking:
        amount = max(amount for amount, _, _ in evidence.asking)
        _, item, excerpt = next(
            (entry for entry in evidence.asking if entry[0] == amount)
        )
        if amount >= HIGH_VALUE_THRESHOLD:
            assessment.score = 1.0
            tier = "at or above $10M"
        elif amount >= MID_VALUE_THRESHOLD:
            assessment.score = 0.6
            tier = "at or above $1M"
        else:
            assessment.score = 0.3
            tier = "below $1M"
        assessment.evidence.append(_ref(item, excerpt))
        assessment.explanation = (
            f"attributed current asking price {amount} ({tier}); "
            "asking price signals transaction scale, not investment quality"
        )
        assessment.historical = _is_historical(item)
    elif evidence.scale_money:
        kind, item, excerpt = evidence.scale_money[0]
        assessment.score = 0.1
        assessment.evidence.append(_ref(item, excerpt))
        assessment.historical = True
        assessment.explanation = (
            f"only {kind} evidence; weak scale signal, not a current asking price"
        )
    else:
        assessment.unknowns.append("asking price and transaction scale unknown")
        assessment.explanation = "value unknown; factor excluded from scoring, not scored zero"
    return assessment


def _score_distress(evidence: _CandidateEvidence) -> FactorAssessment:
    assessment = FactorAssessment(factor="distress", weight=WEIGHT_DISTRESS)
    if not evidence.distress:
        assessment.unknowns.append("financial distress evidence unknown")
        assessment.explanation = "distress unknown; factor excluded from scoring, not scored zero"
        return assessment
    severe = [name for name in evidence.distress if name in _SEVERE_DISTRESS]
    assessment.score = 1.0 if severe else 0.6
    for name, (item, excerpt) in sorted(evidence.distress.items()):
        assessment.evidence.append(_ref(item, f"[{name}] {excerpt}"))
        assessment.historical = assessment.historical or _is_historical(item)
    assessment.explanation = (
        f"attributed distress signals: {', '.join(sorted(evidence.distress))}"
    )
    return assessment


def _score_underuse(evidence: _CandidateEvidence) -> FactorAssessment:
    assessment = FactorAssessment(factor="underuse", weight=WEIGHT_UNDERUSE)
    candidates: list[tuple[float, str]] = []
    for item, observation in evidence.occupancy:
        assessment.evidence.append(_ref(item, observation.evidence))
        assessment.historical = assessment.historical or _is_historical(item)
        if observation.occupancy_pct is not None:
            pct = observation.occupancy_pct
            if pct < 50:
                candidates.append((1.0, f"occupancy {pct}% below 50%"))
            elif pct <= 80:
                candidates.append((0.5, f"occupancy {pct}% between 50-80%"))
            else:
                candidates.append((-0.6, f"occupancy {pct}% above 80% (supported negative)"))
        if observation.vacancy_pct is not None:
            pct = observation.vacancy_pct
            if pct >= 30:
                candidates.append((1.0, f"vacancy {pct}% at or above 30%"))
            elif pct >= 10:
                candidates.append((0.5, f"vacancy {pct}% between 10-30%"))
            else:
                candidates.append((-0.5, f"vacancy {pct}% below 10% (supported negative)"))
    for name, (item, excerpt) in sorted(evidence.underuse.items()):
        assessment.evidence.append(_ref(item, f"[{name}] {excerpt}"))
        assessment.historical = assessment.historical or _is_historical(item)
        if name == "high_vacancy":
            candidates.append((1.0, "explicit high-vacancy evidence"))
        elif name == "low_occupancy":
            candidates.append((0.7, "explicit low-occupancy evidence"))
        else:
            candidates.append((0.5, f"underuse signal '{name}'"))
    if not candidates and not assessment.evidence:
        assessment.unknowns.append("occupancy, vacancy, and underuse evidence unknown")
        assessment.explanation = "underuse unknown; factor excluded from scoring, not scored zero"
        return assessment
    best_score, best_reason = max(candidates, key=lambda entry: entry[0]) if candidates else (0.0, "")
    assessment.score = best_score
    assessment.explanation = best_reason or "underuse signals present without figures"
    return assessment


def _score_building(items: list[ResultIntelligence], members: list[int]) -> FactorAssessment:
    assessment = FactorAssessment(factor="building", weight=WEIGHT_BUILDING)
    for index in members:
        item = items[index]
        if len(item.properties) != 1:
            continue
        for prop in item.properties:
            if prop.category is not None:
                assessment.score = 0.5
                assessment.evidence.append(_ref(item, prop.evidence or prop.name or "typed structure"))
                assessment.explanation = (
                    f"convertible commercial type evidence ({prop.category.value}); "
                    "no floors, loads, or systems assessed (Raphael/Onesimus scope)"
                )
                return assessment
    assessment.unknowns.append("building-type evidence unknown")
    assessment.explanation = "building characteristics unknown; factor excluded from scoring"
    return assessment


def _score_evidence(evidence: _CandidateEvidence) -> FactorAssessment:
    assessment = FactorAssessment(factor="evidence", weight=WEIGHT_EVIDENCE)
    if evidence.contradictions:
        assessment.score = -0.5
        assessment.explanation = "contradictory evidence requires reconciliation before trust"
        return assessment
    total = 0.0
    parts: list[str] = []
    if evidence.has_current_evidence:
        total += 0.4
        parts.append("recent evidence present")
    if len(evidence.domains) >= 2:
        total += 0.4
        parts.append(f"{len(evidence.domains)} distinct source domains (repeat appearances are not corroboration)")
    else:
        parts.append("single source domain; no independent corroboration")
    total += 0.2
    parts.append("no contradictions detected")
    assessment.score = round(min(1.0, total), 3)
    assessment.explanation = "; ".join(parts)
    return assessment


def _score_opportunity(
    evidence: _CandidateEvidence,
    items: list[ResultIntelligence],
    downtown: bool,
) -> OpportunityAssessment:
    factors = [
        _score_value(evidence),
        _score_distress(evidence),
        _score_underuse(evidence),
        _score_building(items, evidence.members),
        _score_evidence(evidence),
    ]
    evaluated = [factor for factor in factors if factor.score is not None]
    evaluated_weight = sum(factor.weight for factor in evaluated)
    completeness = round(evaluated_weight, 3)
    assessment = OpportunityAssessment(completeness=completeness, factors=factors)
    if completeness < COMPLETENESS_FLOOR:
        return assessment  # score stays None: insufficient evidence for comparison
    total = sum(factor.score * factor.weight for factor in evaluated if factor.score is not None)
    score = total / evaluated_weight
    if downtown:
        assessment.downtown_bonus = DOWNTOWN_BONUS
        score += DOWNTOWN_BONUS
    assessment.score = round(score, 3)
    assessment.sufficient = True
    return assessment


def _source_roles(candidate: RankedCandidate, contradiction_urls: set[str]) -> dict[str, list[str]]:
    """Conservative per-candidate source roles (observation only).

    ``claim_evidence`` requires the URL to appear in a surfaced evidence
    reference; ``contradictory_evidence`` requires an explicit
    contradiction relationship recorded during collection; anything else
    in ``sources`` is ``member_only``. Roles never affect scoring,
    grouping, or identity. Ordering follows ``sources``; role lists
    are sorted.
    """
    claimed: set[str] = set()
    if candidate.value_detail is not None:
        claimed.add(candidate.value_detail.url)
    for factor in candidate.opportunity.factors:
        claimed.update(ref.url for ref in factor.evidence)
    claimed.update(ref.url for ref in candidate.distress)
    claimed.update(ref.url for ref in candidate.underuse)
    claimed.update(record.source_url for record in candidate.exclusions)
    claimed.update(record.source_url for record in candidate.deferrals)
    roles: dict[str, list[str]] = {}
    for url in candidate.sources:
        assigned = sorted(
            (["claim_evidence"] if url in claimed else [])
            + (["contradictory_evidence"] if url in contradiction_urls else [])
        )
        roles[url] = assigned or ["member_only"]
    return roles


# ---------------------------------------------------------------------------
# Ranking assembly
# ---------------------------------------------------------------------------


def _candidate_identity(
    items: list[ResultIntelligence], members: list[int], group_key: str
) -> tuple[str | None, str | None, str | None]:
    identity = assembled_identity(items, members, group_key)
    return identity["name"], identity["city"], identity["state"]


def _signal_refs(
    refs: list[tuple[str, ResultIntelligence, str]]
) -> list[SignalRef]:
    # Every occurrence retained; scoring deduplicates by signal name.
    return [
        SignalRef(signal=name, evidence=excerpt[:300], url=item.result_url, date=_item_date(item))
        for name, item, excerpt in sorted(refs, key=lambda entry: (entry[0], entry[1].result_url))
    ]


def rank_intelligence(intel: ResearchIntelligence) -> PropertyRanking:
    """Rank identifiable candidates from extractor intelligence (pure)."""
    items = intel.results
    ranking = PropertyRanking()
    for group in group_by_property(items):
        if group.key.startswith("ungrouped:"):
            index = group.members[0]
            item = items[index]
            land = any(s.signal in ("land_vacant", "land_parcel") for s in item.structure)
            if land:
                ranking.screened_leads.append(
                    ScreenedLead(
                        result_url=item.result_url,
                        title=item.title,
                        rule="unsuitable_structure",
                        reason="land or parcel evidence without an identifiable convertible building",
                    )
                )
            else:
                ranking.screened_leads.append(
                    ScreenedLead(
                        result_url=item.result_url,
                        title=item.title,
                        rule="not_identifiable_property",
                        reason=f"{item.classification.value} without an identifiable property candidate",
                    )
                )
            continue
        evidence = _collect_candidate(group.key, group.members, items)
        for reason in group.review_reasons:
            evidence.unresolved.append(f"identity review: {reason}")
        if group.needs_review and not group.review_reasons:
            evidence.unresolved.append("identity review: grouped observations require identity review")
        if any(len(items[index].properties) > 1 for index in group.members):
            evidence.unresolved.append(
                "source discusses multiple properties; shared observations left unattributed for review"
            )
        disputes = identity_disputes(items, group.members, group.key)
        for field in sorted(disputes):
            if field in ("name", "unit_designator"):
                continue  # grouping already reports these conflicts for review
            quoted = ", ".join(f"'{value}'" for value in disputes[field])
            evidence.unresolved.append(f"identity review: conflicting observed {field}: {quoted}")
        name, city, state = _candidate_identity(items, group.members, group.key)
        county, county_evidence = _assess_county(evidence)
        downtown = bool(evidence.downtown_hits)
        candidate = RankedCandidate(
            key=group.key,
            name=name,
            city=city,
            state=state,
            identity_disputes=disputes,
            county=county,
            county_evidence=county_evidence,
            downtown=downtown,
            distress=_signal_refs(evidence.distress_refs),
            underuse=_signal_refs(evidence.underuse_refs),
            sources=sorted({items[index].result_url for index in group.members}),
        )
        _evaluate_qc(candidate, evidence, items)
        candidate.next_steps = _next_steps(candidate, evidence)
        if group.needs_review:
            candidate.next_steps.append(
                "Raphael: confirm the grouped sources describe a single building before relying on attributed facts"
            )
        if (
            evidence.asking
            and not evidence.asking_contested
            and candidate.qc_status is not QCStatus.EXCLUDE
        ):
            amount = max(entry[0] for entry in evidence.asking)
            _, item, excerpt = next(entry for entry in evidence.asking if entry[0] == amount)
            candidate.value_detail = ValueDetail(
                amount=str(amount), evidence=excerpt[:300], url=item.result_url, date=_item_date(item)
            )
        candidate.opportunity = _score_opportunity(evidence, items, downtown)
        candidate.source_roles = _source_roles(candidate, evidence.contradiction_urls)
        ranking.candidates.append(candidate)
    # Value ranking: priced candidates in the active queue, by asking price.
    # Deferred out-of-area leads keep their evidence but leave the queue.
    queued = [
        candidate for candidate in ranking.candidates
        if candidate.qc_status not in (QCStatus.EXCLUDE, QCStatus.DEFERRED)
    ]
    priced = [candidate for candidate in queued if candidate.value_detail is not None]
    priced.sort(key=lambda candidate: (Decimal(candidate.value_detail.amount), candidate.key), reverse=True)  # type: ignore[union-attr]
    for rank, candidate in enumerate(priced, start=1):
        candidate.value_rank = rank
        ranking.value_order.append(candidate.key)
    ranking.unpriced = sorted(
        candidate.key for candidate in queued if candidate.value_detail is None
    )
    # Opportunity ranking: sufficient queued candidates by score, with a
    # geographic tiebreak only (primary > secondary > unknown).
    scored = [
        candidate for candidate in queued
        if candidate.opportunity.sufficient and candidate.opportunity.score is not None
    ]
    scored.sort(
        key=lambda candidate: (candidate.opportunity.score, _geo_priority(candidate.county), candidate.key),  # type: ignore[misc]
        reverse=True,
    )
    ranking.opportunity_order = [candidate.key for candidate in scored]
    ranking.insufficient_data = sorted(
        candidate.key for candidate in queued if not candidate.opportunity.sufficient
    )
    return ranking
