"""Twelve-county geographic regression tests (B5-1 expansion).

Deterministic; no network, no database. Dallas County is the primary
territory, eleven secondary counties are eligible territory, unknown
county stays eligible, and confirmed out-of-area leads are deferred
with evidence retained -- never silently discarded.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
from app.agents.gabriel.ranking import (
    CountyEligibility,
    QCStatus,
    rank_intelligence,
)
from app.agents.gabriel.sources.brave import BraveSearchResult

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0


def _result(title, snippet, url="https://example.com/story", published_at=None):
    global _RANK
    _RANK += 1
    return BraveSearchResult(
        assignment_id="gsp-geo001",
        query="test query Texas county",
        title=title,
        url=url,
        snippet=snippet,
        domain=urlparse(url).netloc,
        provider="brave",
        retrieved_at=_RETRIEVED,
        published_at=published_at,
        rank=_RANK,
    )


def _rank(*results):
    intel = ResearchIntelligence(results=[extract_intelligence(item) for item in results])
    return rank_intelligence(intel)


def _only_candidate(ranking):
    assert len(ranking.candidates) == 1
    return ranking.candidates[0]


# --- 0. Shared priority order is exact ---


def test_county_priority_order_is_exact() -> None:
    from app.agents.gabriel.geography import (
        COUNTY_PRIORITY_ORDER,
        SECONDARY_COUNTIES,
        ordered_county_names,
    )

    assert COUNTY_PRIORITY_ORDER == (
        "dallas county",
        "tarrant county",
        "collin county",
        "denton county",
        "ellis county",
        "johnson county",
        "kaufman county",
        "parker county",
        "rockwall county",
        "hunt county",
        "wise county",
        "navarro county",
    )
    assert set(COUNTY_PRIORITY_ORDER[1:]) == set(SECONDARY_COUNTIES)
    assert ordered_county_names()[0] == "Dallas County"
    assert len(ordered_county_names()) == 12


# --- 1. Dallas County eligibility (primary territory) ---


def test_dallas_county_primary_area() -> None:
    ranking = _rank(
        _result(
            "Commerce Tower listed",
            "Commerce Tower at 100 Main St, Dallas, TX is listed for sale. "
            "The Dallas County office tower seeks buyers.",
            url="https://example.com/commerce",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.PRIMARY_AREA
    assert candidate.qc_status is not QCStatus.EXCLUDE
    assert candidate.qc_status is not QCStatus.DEFERRED
    assert "primary territory" in candidate.county_evidence


# --- 2. Each secondary county is eligible secondary territory ---


@pytest.mark.parametrize(
    "county,city",
    [
        ("Tarrant County", "Fort Worth"),
        ("Collin County", "Plano"),
        ("Denton County", "Denton"),
        ("Ellis County", "Waxahachie"),
        ("Johnson County", "Cleburne"),
        ("Kaufman County", "Kaufman"),
        ("Parker County", "Weatherford"),
        ("Rockwall County", "Rockwall"),
        ("Hunt County", "Greenville"),
        ("Wise County", "Decatur"),
        ("Navarro County", "Corsicana"),
    ],
)
def test_secondary_counties_secondary_area(county: str, city: str) -> None:
    ranking = _rank(
        _result(
            f"{city} offices listed",
            f"Beacon Tower at 100 Beacon St, {city}, TX is listed for sale. "
            f"The {county} office tower seeks buyers.",
            url=f"https://example.com/{city.lower().replace(' ', '')}",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.SECONDARY_AREA
    assert candidate.qc_status is not QCStatus.EXCLUDE
    assert candidate.qc_status is not QCStatus.DEFERRED
    assert candidate.exclusions == []
    assert "secondary territory" in candidate.county_evidence


# --- 3. Unknown county stays eligible ---


def test_unknown_county_retained_for_investigation() -> None:
    ranking = _rank(
        _result(
            "Summit Tower listed",
            "Summit Tower in Dallas, TX is listed for sale with an asking price of $9 million.",
            url="https://example.com/summit",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.COUNTY_UNKNOWN
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED
    assert candidate.key in ranking.unpriced or candidate.value_rank == 1


# --- 4. Conflicting county evidence ---


def test_conflicting_county_evidence_stays_unknown() -> None:
    ranking = _rank(
        _result(
            "Metro portfolio spans counties",
            "The Metro portfolio spans Dallas County and Harris County holdings, "
            "including Metro Tower.",
            url="https://example.com/metro",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.COUNTY_UNKNOWN
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED
    assert any("county" in text.lower() for text in candidate.unresolved)


# --- 5. Municipality spanning county boundaries ---


def test_cross_county_municipality_uses_county_evidence() -> None:
    ranking = _rank(
        _result(
            "Carrollton offices listed",
            "Lakepoint offices at 2000 Lakepoint Dr, Carrollton, TX are listed "
            "for sale. The Denton County property includes two floors.",
            url="https://example.com/lakepoint",
        )
    )
    candidate = _only_candidate(ranking)
    # Carrollton straddles county lines; the explicit county mention decides.
    assert candidate.county is CountyEligibility.SECONDARY_AREA
    assert "denton county" in candidate.county_evidence


def test_cross_county_municipality_alone_stays_unknown() -> None:
    ranking = _rank(
        _result(
            "Irving Tower listed",
            "Irving Tower in Irving, TX is listed for sale.",
            url="https://example.com/irving-tower",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.COUNTY_UNKNOWN


# --- 6. Confirmed out-of-area property deferred, evidence retained ---


def test_outside_operating_area_deferred_not_discarded() -> None:
    ranking = _rank(
        _result(
            "Houston tower listed",
            "Bayou Tower at 500 Smith St, Houston, TX is listed for sale with an "
            "asking price of $50 million. The Harris County office tower drew bids.",
            url="https://example.com/bayou",
            published_at="2026-10-20",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.OUTSIDE_OPERATING_AREA
    assert candidate.qc_status is QCStatus.DEFERRED
    assert len(candidate.deferrals) == 1
    deferral = candidate.deferrals[0]
    assert deferral.rule == "outside_operating_area"
    assert "Harris" in deferral.evidence
    assert deferral.source_url == "https://example.com/bayou"
    assert deferral.why_sufficient
    assert deferral.identity
    # Evidence retained on the candidate but out of the active queue.
    assert candidate.value_detail is not None
    assert candidate.value_detail.amount == "50000000"
    assert candidate.key not in ranking.value_order
    assert candidate.key not in ranking.opportunity_order
    assert candidate.key not in ranking.unpriced
    assert candidate.key not in ranking.insufficient_data


# --- 7. Downtown preference consistent across municipalities ---


def test_downtown_bonus_applies_outside_dallas() -> None:
    ranking = _rank(
        _result(
            "Downtown Fort Worth tower listed",
            "Panther Tower at 100 Throckmorton St, Fort Worth, TX is listed for sale "
            "with an asking price of $30 million. The downtown Tarrant County "
            "office tower is fully leased.",
            url="https://example.com/panther",
            published_at="2026-10-20",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.SECONDARY_AREA
    assert candidate.downtown is True
    assert candidate.opportunity.downtown_bonus == 0.05
    assert candidate.opportunity.sufficient is True


# --- 8. Strong neighboring-county opportunity outranks weaker Dallas one ---


def test_tarrant_distressed_outranks_occupied_dallas() -> None:
    ranking = _rank(
        _result(
            "Fort Worth tower bankruptcy sale",
            "Beacon Tower at 900 Commerce St, Fort Worth, TX is listed for sale with "
            "an asking price of $60 million after the owner's bankruptcy filing. "
            "The Tarrant County office tower operates at 35% occupancy across the "
            "entire building.",
            url="https://example.com/beacon",
            published_at="2026-10-20",
        ),
        _result(
            "Dallas tower fully leased",
            "Elm Tower at 100 Elm St, Dallas, TX is listed for sale with an asking "
            "price of $15 million. The Dallas County office tower is fully occupied "
            "at 100% occupancy across the entire building.",
            url="https://example.com/elm",
            published_at="2026-10-20",
        ),
    )
    assert len(ranking.candidates) == 2
    assert len(ranking.opportunity_order) == 2
    tarrant = next(c for c in ranking.candidates if c.county is CountyEligibility.SECONDARY_AREA)
    dallas = next(c for c in ranking.candidates if c.county is CountyEligibility.PRIMARY_AREA)
    assert ranking.opportunity_order[0] == tarrant.key
    assert tarrant.opportunity.score is not None
    assert dallas.opportunity.score is not None
    assert tarrant.opportunity.score > dallas.opportunity.score
