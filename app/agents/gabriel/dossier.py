"""Evidence dossier builder (MVP-004B-5C).

Turns multi-round search intelligence into coherent per-property
research dossiers: consolidated claims, cross-source corroboration,
temporal-aware contradiction detection, and structured agent handoffs.

Pure and deterministic: no network calls, no database writes, no AI
calls. Ranking reassessment reuses the existing MVP-004B-4/5 ranking
engine -- there is deliberately no second scoring algorithm here.
"""

from __future__ import annotations

import re
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from app.agents.gabriel.extractor import (
    MoneyKind,
    ResearchIntelligence,
    ResultIntelligence,
    TemporalStatus,
    assembled_identity,
    group_by_property,
)
from app.agents.gabriel.ranking import (
    CountyEligibility,
    ExclusionRecord,
    PropertyRanking,
    QCStatus,
    RankedCandidate,
    rank_intelligence,
)

DOSSIER_VERSION = "004B-5C.0"

MAX_ROUNDS = 10
MAX_ROUND_RESULTS = 200
MAX_EXECUTED_QUERIES = 50


class DossierRequest(BaseModel):
    """Rounds of intelligence to consolidate (round 0 is initial research)."""

    rounds: list[ResearchIntelligence] = Field(min_length=1, max_length=MAX_ROUNDS)
    prior_ranking: PropertyRanking | None = None
    executed_queries: list[str] = Field(default_factory=list, max_length=MAX_EXECUTED_QUERIES)
    correlation_id: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _bound_total_results(self):
        total = sum(len(item.results) for item in self.rounds)
        if total > MAX_ROUND_RESULTS:
            raise ValueError(f"too many results across rounds (max {MAX_ROUND_RESULTS})")
        return self


class ClaimStatus(str, Enum):
    SUPPORTED = "supported"  # >=2 independent domains, non-identical reports
    SINGLE_SOURCE = "single_source"
    HISTORICAL = "historical"
    CONTRADICTORY = "contradictory"


class ClaimEvidence(BaseModel):
    excerpt: str
    url: str
    domain: str = ""
    round: int = 0
    date: str | None = None


class ConsolidatedClaim(BaseModel):
    topic: str
    statement: str
    status: ClaimStatus = ClaimStatus.SINGLE_SOURCE
    verification: str = "reported_unverified"
    evidence: list[ClaimEvidence] = []
    corroboration_note: str = ""


class DossierContradiction(BaseModel):
    topic: str
    description: str
    evidence: list[ClaimEvidence] = []
    resolution: str = ""


class AgentHandoff(BaseModel):
    agent: str
    questions: list[str] = []


class PropertyDossier(BaseModel):
    key: str
    name: str | None = None
    street_address: str | None = None
    city: str | None = None
    state: str | None = None
    identity_disputes: dict[str, list[str]] = Field(default_factory=dict)
    county: CountyEligibility = CountyEligibility.COUNTY_UNKNOWN
    county_evidence: str = ""
    qc_status: QCStatus = QCStatus.RESEARCH_REQUIRED
    exclusions: list[ExclusionRecord] = []
    deferrals: list[ExclusionRecord] = []
    value_amount: str | None = None
    value_note: str = ""
    opportunity_score: float | None = None
    opportunity_completeness: float = 0.0
    opportunity_sufficient: bool = False
    supported_facts: list[ConsolidatedClaim] = []
    historical_observations: list[ConsolidatedClaim] = []
    distress_findings: list[ConsolidatedClaim] = []
    underuse_findings: list[ConsolidatedClaim] = []
    sources: list[str] = []
    conflicts: list[DossierContradiction] = []
    missing: list[str] = []
    actions_performed: list[str] = []
    remaining_questions: list[str] = []
    handoffs: list[AgentHandoff] = []
    summary: str = ""


class RankingComparison(BaseModel):
    candidate_key: str
    prior_score: float | None = None
    new_score: float | None = None
    delta: float | None = None
    notes: list[str] = []


class DossierSet(BaseModel):
    dossiers: list[PropertyDossier] = []
    ranking: PropertyRanking = PropertyRanking()
    comparison: list[RankingComparison] = []
    dossier_version: str = DOSSIER_VERSION
    rounds_executed: int = 0
    total_results: int = 0
    run_id: str = ""
    correlation_id: str = ""


_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_WS_RE = re.compile(r"\s+")


def _result_date(item: ResultIntelligence) -> str | None:
    return item.temporal.published_at or item.retrieved_at or None


def _year_of(date: str | None) -> int | None:
    if not date:
        return None
    match = _YEAR_RE.search(date)
    return int(match.group(0)) if match else None


def _is_historical(item: ResultIntelligence) -> bool:
    return item.temporal.status is TemporalStatus.HISTORICAL


def _norm_text(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def _single_property(item: ResultIntelligence) -> bool:
    return len(item.properties) == 1


# ---------------------------------------------------------------------------
# Round-aware evidence collection (attribution mirrors the ranking engine)
# ---------------------------------------------------------------------------


class _DossierEvidence:
    def __init__(self) -> None:
        self.asking: list[tuple[Decimal, ResultIntelligence, int, str]] = []
        self.history_money: list[tuple[str, Decimal, ResultIntelligence, int, str]] = []
        self.occupancy: list[tuple[ResultIntelligence, int, object]] = []
        self.availability: list[tuple[ResultIntelligence, int, object]] = []
        self.distress: list[tuple[str, ResultIntelligence, int, str]] = []
        self.underuse: list[tuple[str, ResultIntelligence, int, str]] = []
        self.counties: list[tuple[str, ResultIntelligence, int]] = []
        self.structure: dict[str, tuple[ResultIntelligence, int, str]] = {}


def _collect_round_evidence(
    members: list[int], items: list[ResultIntelligence], rounds: list[int]
) -> _DossierEvidence:
    evidence = _DossierEvidence()
    for index in members:
        item = items[index]
        round_no = rounds[index]
        if not _single_property(item):
            continue  # multi-property observations stay unattributed
        for mention in item.money:
            if mention.amount_high is not None:
                continue  # quoted ranges stay unresolved (surfaced via missing[])
            if mention.kind is MoneyKind.ASKING_PRICE_CURRENT and not _is_historical(item):
                evidence.asking.append((mention.amount, item, round_no, mention.evidence))
            elif mention.kind is not MoneyKind.UNCLASSIFIED:
                evidence.history_money.append(
                    (mention.kind.value, mention.amount, item, round_no, mention.evidence)
                )
        for observation in item.occupancy:
            if observation.scope.value == "whole_building":
                evidence.occupancy.append((item, round_no, observation))
        for observation in item.availability:
            evidence.availability.append((item, round_no, observation))
        for signal in item.distress:
            evidence.distress.append((signal.signal, item, round_no, signal.evidence))
        for signal in item.underuse:
            evidence.underuse.append((signal.signal, item, round_no, signal.evidence))
        for county in item.county_mentions:
            evidence.counties.append((county, item, round_no))
        for signal in item.structure:
            if signal.signal not in evidence.structure:
                evidence.structure[signal.signal] = (item, round_no, signal.evidence)
    return evidence


def _ref(item: ResultIntelligence, round_no: int, excerpt: str) -> ClaimEvidence:
    return ClaimEvidence(
        excerpt=excerpt[:300],
        url=item.result_url,
        domain=item.domain or item.result_url,
        round=round_no,
        date=_result_date(item),
    )


def _corroboration(
    refs: list[ClaimEvidence],
) -> tuple[ClaimStatus, str]:
    domains = {ref.domain for ref in refs}
    if len(refs) >= 2 and len(domains) >= 2:
        texts = {_norm_text(ref.excerpt) for ref in refs}
        if len(texts) == 1:
            return (
                ClaimStatus.SINGLE_SOURCE,
                "identical text repeated across domains: republication, not independent corroboration",
            )
        return (
            ClaimStatus.SUPPORTED,
            f"{len(domains)} distinct domains with non-identical reports",
        )
    if len(refs) >= 2:
        return ClaimStatus.SINGLE_SOURCE, "repeated within one domain: repetition is not corroboration"
    return ClaimStatus.SINGLE_SOURCE, "single source; independent corroboration needed"


# ---------------------------------------------------------------------------
# Claim consolidation
# ---------------------------------------------------------------------------


def _asking_claims(evidence: _DossierEvidence) -> tuple[list[ConsolidatedClaim], list[ConsolidatedClaim]]:
    """Current askings become supported facts; dated older amounts stay history."""
    supported: list[ConsolidatedClaim] = []
    historical: list[ConsolidatedClaim] = []
    if not evidence.asking:
        return supported, historical
    by_amount: dict[str, list[ClaimEvidence]] = {}
    for amount, item, round_no, excerpt in evidence.asking:
        by_amount.setdefault(str(amount), []).append(_ref(item, round_no, excerpt))
    if len(by_amount) == 1:
        amount, refs = next(iter(by_amount.items()))
        status, note = _corroboration(refs)
        supported.append(
            ConsolidatedClaim(
                topic="asking_price",
                statement=f"Current asking price {amount} (asking price, not a market valuation).",
                status=status,
                evidence=refs,
                corroboration_note=note,
            )
        )
        return supported, historical
    # Multiple distinct amounts: same-period conflicts stay supported-but-
    # contradictory (flagged below); different-year amounts show change.
    dated = sorted(
        ((amount, _year_of(ref.date), ref) for amount, refs in by_amount.items() for ref in refs),
        key=lambda entry: (entry[1] or 0, entry[0]),
    )
    years = {year for _, year, _ in dated if year is not None}
    if len(years) >= 2:
        oldest = min(dated, key=lambda entry: (entry[1] or 0))
        newest_year = max(years)
        newest_entries = [(amount, ref) for amount, year, ref in dated if year == newest_year]
        newest_amounts = sorted({amount for amount, _ in newest_entries})
        if len(newest_amounts) > 1:
            # Newest-year disagreement: contradictory, not a clean current price.
            historical.append(
                ConsolidatedClaim(
                    topic="asking_price",
                    statement=f"Asking price {oldest[0]} reported in {oldest[1]}; "
                    f"later asking reports in {newest_year} disagree, see conflicts.",
                    status=ClaimStatus.HISTORICAL,
                    evidence=[oldest[2]],
                )
            )
            for amount in newest_amounts:
                refs = [ref for candidate, ref in newest_entries if candidate == amount]
                supported.append(
                    ConsolidatedClaim(
                        topic="asking_price",
                        statement=f"Asking price {amount} reported in {newest_year} "
                        "(conflicts with other asking reports).",
                        status=ClaimStatus.CONTRADICTORY,
                        evidence=refs,
                        corroboration_note="see conflicts; Raphael must reconcile",
                    )
                )
            return supported, historical
        newest = max(dated, key=lambda entry: (entry[1] or 0))
        historical.append(
            ConsolidatedClaim(
                topic="asking_price",
                statement=f"Asking price {oldest[0]} reported in {oldest[1]}; later asking {newest[0]} "
                f"in {newest[1]} indicates a price change, not a contradiction.",
                status=ClaimStatus.HISTORICAL,
                evidence=[oldest[2]],
            )
        )
        current_refs = [ref for amount, year, ref in dated if year == newest[1]]
        status, note = _corroboration(current_refs)
        supported.append(
            ConsolidatedClaim(
                topic="asking_price",
                statement=f"Current asking price {newest[0]} (asking price, not a market valuation).",
                status=status,
                evidence=current_refs,
                corroboration_note=note,
            )
        )
        return supported, historical
    for amount, refs in sorted(by_amount.items()):
        supported.append(
            ConsolidatedClaim(
                topic="asking_price",
                statement=f"Asking price {amount} reported (conflicts with other asking reports).",
                status=ClaimStatus.CONTRADICTORY,
                evidence=refs,
                corroboration_note="see conflicts; Raphael must reconcile",
            )
        )
    return supported, historical


def _occupancy_claims(evidence: _DossierEvidence) -> tuple[list[ConsolidatedClaim], list[ConsolidatedClaim]]:
    supported: list[ConsolidatedClaim] = []
    historical: list[ConsolidatedClaim] = []
    current: dict[str, list[ClaimEvidence]] = {}
    for item, round_no, observation in evidence.occupancy:  # type: ignore[union-attr]
        if observation.occupancy_pct is not None:
            label = f"occupancy {observation.occupancy_pct}%"
        elif observation.vacancy_pct is not None:
            label = f"vacancy {observation.vacancy_pct}%"
        else:
            continue
        period = observation.period or ""
        target = historical if _is_historical(item) else None
        ref = _ref(item, round_no, observation.evidence)
        if target is not None:
            target.append(
                ConsolidatedClaim(
                    topic="occupancy",
                    statement=f"Historical {label} ({observation.evidence[:120]}).",
                    status=ClaimStatus.HISTORICAL,
                    evidence=[ref],
                )
            )
            continue
        current.setdefault(f"{label}|{period}", []).append(ref)
    for key, refs in sorted(current.items()):
        label = key.split("|")[0]
        status, note = _corroboration(refs)
        supported.append(
            ConsolidatedClaim(
                topic="occupancy",
                statement=f"Reported whole-building {label} (reported, not verified).",
                status=status,
                evidence=refs,
                corroboration_note=note,
            )
        )
    return supported, historical


def _signal_claims(
    refs: list[tuple[str, ResultIntelligence, int, str]], topic: str, label: str
) -> tuple[list[ConsolidatedClaim], list[ConsolidatedClaim]]:
    findings: list[ConsolidatedClaim] = []
    historical: list[ConsolidatedClaim] = []
    by_signal: dict[str, list[tuple[ClaimEvidence, bool]]] = {}
    for signal, item, round_no, excerpt in refs:
        by_signal.setdefault(signal, []).append((_ref(item, round_no, excerpt), _is_historical(item)))
    for signal in sorted(by_signal):
        entries = by_signal[signal]
        current_refs = [ref for ref, old in entries if not old]
        old_refs = [ref for ref, old in entries if old]
        if current_refs:
            status, note = _corroboration(current_refs)
            findings.append(
                ConsolidatedClaim(
                    topic=f"{topic}:{signal}",
                    statement=f"{label} signal '{signal}' reported (reported, not verified).",
                    status=status,
                    evidence=current_refs,
                    corroboration_note=note,
                )
            )
        for ref in old_refs:
            historical.append(
                ConsolidatedClaim(
                    topic=f"{topic}:{signal}",
                    statement=f"Historical {label.lower()} signal '{signal}' (does not establish current status).",
                    status=ClaimStatus.HISTORICAL,
                    evidence=[ref],
                )
            )
    return findings, historical


def _availability_claims(evidence: _DossierEvidence) -> list[ConsolidatedClaim]:
    claims: list[ConsolidatedClaim] = []
    for item, round_no, observation in evidence.availability:  # type: ignore[union-attr]
        claims.append(
            ConsolidatedClaim(
                topic="availability",
                statement=f"Advertised {observation.square_feet} sq ft available for lease "
                "(availability is NOT physical vacancy).",
                status=ClaimStatus.SINGLE_SOURCE,
                evidence=[_ref(item, round_no, observation.evidence)],
            )
        )
    return claims


def _history_money_claims(evidence: _DossierEvidence) -> list[ConsolidatedClaim]:
    claims: list[ConsolidatedClaim] = []
    for kind, amount, item, round_no, excerpt in evidence.history_money:
        claims.append(
            ConsolidatedClaim(
                topic="transaction_history",
                statement=f"{kind} of {amount} (historical context, not a current asking price).",
                status=ClaimStatus.HISTORICAL,
                evidence=[_ref(item, round_no, excerpt)],
            )
        )
    return claims


def _county_claims(evidence: _DossierEvidence) -> list[ConsolidatedClaim]:
    claims: list[ConsolidatedClaim] = []
    by_county: dict[str, list[ClaimEvidence]] = {}
    for county, item, round_no in evidence.counties:
        by_county.setdefault(county.lower(), []).append(_ref(item, round_no, f"county mention '{county}'"))
    for county in sorted(by_county):
        status, note = _corroboration(by_county[county])
        if len(by_county) > 1:
            status = ClaimStatus.CONTRADICTORY
            note = "multiple counties attributed; see conflicts"
        claims.append(
            ConsolidatedClaim(
                topic="county",
                statement=f"County evidence '{county}'.",
                status=status,
                evidence=by_county[county],
                corroboration_note=note,
            )
        )
    return claims


# ---------------------------------------------------------------------------
# Temporal-aware contradiction detection
# ---------------------------------------------------------------------------


def _detect_conflicts(evidence: _DossierEvidence) -> list[DossierContradiction]:
    conflicts: list[DossierContradiction] = []
    amounts = sorted({amount for amount, _, _, _ in evidence.asking})
    if len(amounts) > 1 and amounts[-1] > amounts[0] * Decimal("1.05"):
        years = {
            _year_of(_result_date(item))
            for amount, item, _, _ in evidence.asking
            if _year_of(_result_date(item)) is not None
        }
        if len(years) < 2:
            conflicts.append(
                DossierContradiction(
                    topic="asking_price",
                    description=f"Conflicting asking prices {amounts[0]} vs {amounts[-1]} "
                    "in the same period.",
                    evidence=[
                        _ref(item, round_no, excerpt)
                        for amount, item, round_no, excerpt in evidence.asking
                    ],
                    resolution="Raphael must determine the correct current asking price.",
                )
            )
        else:
            newest_year = max(years)
            newest_amounts = sorted(
                {
                    amount
                    for amount, item, _, _ in evidence.asking
                    if _year_of(_result_date(item)) == newest_year
                }
            )
            if len(newest_amounts) > 1 and newest_amounts[-1] > newest_amounts[0] * Decimal("1.05"):
                conflicts.append(
                    DossierContradiction(
                        topic="asking_price",
                        description=f"Conflicting asking prices {newest_amounts[0]} vs "
                        f"{newest_amounts[-1]} in {newest_year}.",
                        evidence=[
                            _ref(item, round_no, excerpt)
                            for amount, item, round_no, excerpt in evidence.asking
                            if _year_of(_result_date(item)) == newest_year
                        ],
                        resolution="Raphael must determine the correct current asking price.",
                    )
                )
    occupancy_pcts = [
        (observation.occupancy_pct, item, round_no)  # type: ignore[union-attr]
        for item, round_no, observation in evidence.occupancy
        if observation.occupancy_pct is not None  # type: ignore[union-attr]
    ]
    if len({pct for pct, _, _ in occupancy_pcts}) > 1:
        years = {
            _year_of(_result_date(item))
            for _, item, _ in occupancy_pcts
            if _year_of(_result_date(item)) is not None
        }
        if len(years) < 2:
            conflicts.append(
                DossierContradiction(
                    topic="occupancy",
                    description="Conflicting occupancy percentages in the same period.",
                    evidence=[
                        _ref(item, round_no, f"occupancy {pct}%") for pct, item, round_no in occupancy_pcts
                    ],
                    resolution="Raphael must verify current whole-building occupancy.",
                )
            )
    counties = sorted({name.lower() for name, _, _ in evidence.counties})
    if len(counties) > 1:
        conflicts.append(
            DossierContradiction(
                topic="county",
                description=f"Conflicting county evidence ({', '.join(counties)}).",
                evidence=[
                    _ref(item, round_no, f"county mention '{name}'")
                    for name, item, round_no in evidence.counties
                ],
                resolution="Raphael must determine which county holds the property.",
            )
        )
    if "demolished" in evidence.structure and (evidence.asking or evidence.occupancy):
        item, round_no, excerpt = evidence.structure["demolished"]
        conflicts.append(
            DossierContradiction(
                topic="physical_status",
                description="Demolition report conflicts with current asking or occupancy evidence.",
                evidence=[_ref(item, round_no, excerpt)],
                resolution="Raphael must confirm whether the structure still stands.",
            )
        )
    return conflicts


# ---------------------------------------------------------------------------
# Summary, handoffs, comparison
# ---------------------------------------------------------------------------


def _identity_sentence(dossier: PropertyDossier) -> str:
    bits = [bit for bit in (dossier.name, dossier.street_address, dossier.city) if bit]
    identity = ", ".join(bits) if bits else "Unidentified candidate"
    county = {
        CountyEligibility.PRIMARY_AREA: "in Dallas County (primary territory)",
        CountyEligibility.SECONDARY_AREA: "in a secondary county (secondary territory)",
        CountyEligibility.COUNTY_UNKNOWN: "with unknown county",
        CountyEligibility.OUTSIDE_OPERATING_AREA: "outside the ten-county operating area",
    }[dossier.county]
    return f"{identity}, {county}."


def _build_summary(
    dossier: PropertyDossier,
    evidence: _DossierEvidence,
    corroborated: int,
) -> str:
    sentences = [_identity_sentence(dossier)]
    if dossier.value_amount is not None:
        sentences.append(
            f"Asking price {dossier.value_amount} (asking price, not a market valuation)."
        )
    elif any(conflict.topic == "asking_price" for conflict in dossier.conflicts) or any(
        "conflicting asking prices" in note or "price range" in note for note in dossier.missing
    ):
        sentences.append("Asking price is contested or reported as a range; see conflicts and missing evidence.")
    else:
        sentences.append("No asking price documented.")
    distress = sorted({signal for signal, _, _, _ in evidence.distress})
    underuse = sorted({signal for signal, _, _, _ in evidence.underuse})
    sentences.append(
        f"Distress signals: {', '.join(distress)}." if distress else "No distress evidence."
    )
    sentences.append(
        f"Underuse signals: {', '.join(underuse)}." if underuse else "No underuse evidence."
    )
    if dossier.qc_status is QCStatus.EXCLUDE:
        rules = ", ".join(record.rule for record in dossier.exclusions) or "quality control"
        sentences.append(f"Excluded by quality control ({rules}).")
    elif dossier.qc_status is QCStatus.DEFERRED:
        sentences.append("Deferred: outside the operating area; evidence retained.")
    elif dossier.qc_status is QCStatus.PASS:
        sentences.append("Passed preliminary quality control; eligible for deeper investigation.")
    else:
        sentences.append("Requires further research before a meaningful decision.")
    if corroborated:
        sentences.append(f"{corroborated} claim(s) corroborated by independent sources.")
    else:
        sentences.append("No independent corroboration yet; claims are single-source.")
    if dossier.conflicts:
        sentences.append(f"{len(dossier.conflicts)} conflict(s) require reconciliation.")
    return " ".join(sentences)


def _build_handoffs(candidate: RankedCandidate) -> tuple[list[str], list[AgentHandoff]]:
    remaining = list(candidate.next_steps)
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for step in remaining:
        agent, _, rest = step.partition(":")
        agent = agent.strip() or "Gabriel"
        if agent not in grouped:
            grouped[agent] = []
            order.append(agent)
        grouped[agent].append(rest.strip() or step)
    return remaining, [AgentHandoff(agent=agent, questions=grouped[agent]) for agent in order]


def _compare_rankings(prior: PropertyRanking, new: PropertyRanking) -> list[RankingComparison]:
    prior_by_key = {candidate.key: candidate for candidate in prior.candidates}
    new_by_key = {candidate.key: candidate for candidate in new.candidates}
    comparison: list[RankingComparison] = []
    for key in sorted(set(prior_by_key) | set(new_by_key)):
        old = prior_by_key.get(key)
        current = new_by_key.get(key)
        if old is None and current is not None:
            comparison.append(
                RankingComparison(
                    candidate_key=key,
                    new_score=current.opportunity.score,
                    notes=["new candidate since prior ranking"],
                )
            )
            continue
        if current is None and old is not None:
            comparison.append(
                RankingComparison(
                    candidate_key=key,
                    prior_score=old.opportunity.score,
                    notes=["no longer present in reassessed ranking"],
                )
            )
            continue
        assert old is not None and current is not None
        entry = RankingComparison(
            candidate_key=key, prior_score=old.opportunity.score, new_score=current.opportunity.score
        )
        if old.opportunity.score is not None and current.opportunity.score is not None:
            entry.delta = round(current.opportunity.score - old.opportunity.score, 3)
            if entry.delta:
                entry.notes.append(
                    f"score changed from {old.opportunity.score} to {current.opportunity.score}"
                )
        if not old.opportunity.sufficient and current.opportunity.sufficient:
            entry.notes.append("now sufficient for scoring")
        if old.value_detail is None and current.value_detail is not None:
            entry.notes.append(f"asking price newly attributed ({current.value_detail.amount})")
        old_distress = {ref.signal for ref in old.distress}
        new_distress = {ref.signal for ref in current.distress} - old_distress
        if new_distress:
            entry.notes.append(f"new distress evidence: {', '.join(sorted(new_distress))}")
        old_underuse = {ref.signal for ref in old.underuse}
        new_underuse = {ref.signal for ref in current.underuse} - old_underuse
        if new_underuse:
            entry.notes.append(f"new underuse evidence: {', '.join(sorted(new_underuse))}")
        if len(current.sources) > len(old.sources):
            entry.notes.append(
                f"evidence expanded from {len(old.sources)} to {len(current.sources)} source(s)"
            )
        if not entry.notes:
            entry.notes.append("no material change")
        comparison.append(entry)
    return comparison


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_dossiers(request: DossierRequest) -> DossierSet:
    """Consolidate multi-round intelligence into per-property dossiers (pure)."""
    tagged: list[tuple[int, ResultIntelligence]] = [
        (round_no, item)
        for round_no, intel in enumerate(request.rounds)
        for item in intel.results
    ]
    items = [item for _, item in tagged]
    rounds = [round_no for round_no, _ in tagged]
    merged = ResearchIntelligence(results=items)
    ranking = rank_intelligence(merged)  # the existing engine; never a second one
    groups = {group.key: group for group in group_by_property(items)}
    dossier_set = DossierSet(
        ranking=ranking,
        rounds_executed=len(request.rounds),
        total_results=len(items),
    )
    for candidate in ranking.candidates:
        group = groups.get(candidate.key)
        members = group.members if group is not None else []
        evidence = _collect_round_evidence(members, items, rounds)
        asking, asking_history = _asking_claims(evidence)
        occupancy, occupancy_history = _occupancy_claims(evidence)
        distress, distress_history = _signal_claims(evidence.distress, "distress", "Distress")
        underuse, underuse_history = _signal_claims(evidence.underuse, "underuse", "Underuse")
        supported = asking + occupancy + _availability_claims(evidence) + _county_claims(evidence)
        historical = asking_history + occupancy_history + distress_history + underuse_history
        historical += _history_money_claims(evidence)
        conflicts = _detect_conflicts(evidence)
        remaining, handoffs = _build_handoffs(candidate)
        missing = list(dict.fromkeys(candidate.unresolved + [
            unknown for factor in candidate.opportunity.factors for unknown in factor.unknowns
        ]))
        identity = assembled_identity(items, members, candidate.key)
        name = identity["name"]
        address = identity["street_address"]
        city = identity["city"]
        state = identity["state"]
        dossier = PropertyDossier(
            key=candidate.key,
            name=name or candidate.name,
            street_address=address,
            city=city or candidate.city,
            state=state or candidate.state,
            identity_disputes=dict(candidate.identity_disputes),
            county=candidate.county,
            county_evidence=candidate.county_evidence,
            qc_status=candidate.qc_status,
            exclusions=candidate.exclusions,
            deferrals=candidate.deferrals,
            value_amount=candidate.value_detail.amount if candidate.value_detail else None,
            value_note="asking price, not a market valuation" if candidate.value_detail else "",
            opportunity_score=candidate.opportunity.score,
            opportunity_completeness=candidate.opportunity.completeness,
            opportunity_sufficient=candidate.opportunity.sufficient,
            supported_facts=supported,
            historical_observations=historical,
            distress_findings=distress,
            underuse_findings=underuse,
            sources=candidate.sources,
            conflicts=conflicts,
            missing=missing,
            actions_performed=[
                f"Round {n}: {len(intel.results)} result(s)"
                for n, intel in enumerate(request.rounds)
            ] + [f"Executed follow-up query: {query[:160]}" for query in request.executed_queries],
            remaining_questions=remaining,
            handoffs=handoffs,
        )
        corroborated = sum(
            1 for claim in supported + distress + underuse
            if claim.status is ClaimStatus.SUPPORTED
        )
        dossier.summary = _build_summary(dossier, evidence, corroborated)
        dossier_set.dossiers.append(dossier)
    if request.prior_ranking is not None:
        dossier_set.comparison = _compare_rankings(request.prior_ranking, ranking)
    return dossier_set
