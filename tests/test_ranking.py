"""Ranking and quality-control tests (deterministic; no network, no database)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import app.agents.gabriel.research as research
from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
from app.agents.gabriel.ranking import (
    CountyEligibility,
    QCStatus,
    rank_intelligence,
)
from app.agents.gabriel.sources.brave import BraveSearchResult
from app.main import app

client = TestClient(app, raise_server_exceptions=False)

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0


def _result(title, snippet, url="https://example.com/story", published_at=None):
    from urllib.parse import urlparse

    global _RANK
    _RANK += 1
    return BraveSearchResult(
        assignment_id="gsp-test001",
        query="test query Dallas County",
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
    intel = ResearchIntelligence(
        results=[extract_intelligence(item) for item in results]
    )
    return rank_intelligence(intel)


def _only_candidate(ranking):
    assert len(ranking.candidates) == 1
    return ranking.candidates[0]


# --- Test A: high-value listing ---


def test_a_high_value_listing_ranked_by_asking_price() -> None:
    ranking = _rank(
        _result(
            "Meridian Tower listed for sale",
            "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale with "
            "an asking price of $40 million. The Dallas County office tower drew bids.",
            url="https://example.com/meridian-listing",
            published_at="2026-10-20",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.qc_status is QCStatus.PASS
    assert candidate.county is CountyEligibility.PRIMARY_AREA
    assert candidate.value_rank == 1
    assert candidate.value_detail is not None
    assert candidate.value_detail.amount == "40000000"
    assert "not a market valuation" in candidate.value_detail.note
    assert "example.com/meridian-listing" in candidate.value_detail.url
    assert ranking.value_order == [candidate.key]
    value_factor = next(f for f in candidate.opportunity.factors if f.factor == "value")
    assert value_factor.score == 1.0
    assert candidate.opportunity.sufficient is True


# --- Test B: historical transaction is not a current asking price ---


def test_b_historical_transaction_not_current_price() -> None:
    ranking = _rank(
        _result(
            "Gateway Tower sells",
            "Gateway Tower at 8111 LBJ Freeway changed hands in a historical "
            "transaction of approximately $14.1 million.",
            url="https://example.com/gateway-sold",
            published_at="2023-06-01",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.value_detail is None
    assert candidate.value_rank is None
    assert ranking.value_order == []
    assert ranking.unpriced == [candidate.key]
    value_factor = next(f for f in candidate.opportunity.factors if f.factor == "value")
    assert value_factor.score == 0.1  # weak scale signal only


# --- Test C: underutilized office, no distress inferred ---


def test_c_underutilized_office_eligible() -> None:
    ranking = _rank(
        _result(
            "Meridian Tower occupancy falls",
            "Meridian Tower operated at 35% occupancy across the entire building "
            "during Q3 2026 in Dallas County.",
            url="https://example.com/meridian-35",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.qc_status is not QCStatus.EXCLUDE
    assert any(s.signal == "low_occupancy" for s in candidate.underuse)
    assert candidate.distress == []
    distress_factor = next(f for f in candidate.opportunity.factors if f.factor == "distress")
    assert distress_factor.score is None
    underuse_factor = next(f for f in candidate.opportunity.factors if f.factor == "underuse")
    assert underuse_factor.score == 1.0


# --- Test D: vacancy without distress evidence ---


def test_d_vacancy_without_distress() -> None:
    ranking = _rank(
        _result(
            "Parkside Plaza vacancy",
            "Parkside Plaza reports a vacancy rate of 45% across the entire building, Dallas County.",
            url="https://example.com/parkside-vacancy",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.qc_status is not QCStatus.EXCLUDE
    underuse_factor = next(f for f in candidate.opportunity.factors if f.factor == "underuse")
    assert underuse_factor.score == 1.0
    distress_factor = next(f for f in candidate.opportunity.factors if f.factor == "distress")
    assert distress_factor.score is None
    assert "unknown" in " ".join(distress_factor.unknowns).lower()


# --- Test E: adjacent-county property stays eligible (MVP-004B-5 correction) ---


def test_e_adjacent_county_eligible_secondary_area() -> None:
    ranking = _rank(
        _result(
            "Ridgmar Mall offices listed",
            "The Ridgmar office block at 1000 Green Oaks Rd, Fort Worth, TX is listed "
            "for sale, located in Tarrant County.",
            url="https://example.com/ridgmar",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.SECONDARY_AREA
    assert candidate.qc_status is not QCStatus.EXCLUDE
    assert candidate.exclusions == []
    assert "secondary territory" in candidate.county_evidence


# --- Test F: multi-county municipality stays unknown ---


def test_f_municipality_alone_never_assigns_county() -> None:
    ranking = _rank(
        _result(
            "Irving Tower listed",
            "Irving Tower in Irving, TX is listed for sale with an asking price of $5 million.",
            url="https://example.com/irving-tower",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county is CountyEligibility.COUNTY_UNKNOWN
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED
    assert any("county" in step.lower() for step in candidate.next_steps)


# --- Test G: missing price is not exclusion ---


def test_g_missing_price_not_excluded() -> None:
    ranking = _rank(
        _result(
            "Summit Tower profile",
            "Summit Tower at 200 Main St, Dallas, TX operates at 60% occupancy across "
            "the entire building, Dallas County.",
            url="https://example.com/summit-profile",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.value_detail is None
    assert candidate.qc_status is not QCStatus.EXCLUDE
    assert ranking.unpriced == [candidate.key]
    value_factor = next(f for f in candidate.opportunity.factors if f.factor == "value")
    assert value_factor.score is None


# --- Test H: demolished building excluded ---


def test_h_demolished_building_excluded() -> None:
    ranking = _rank(
        _result(
            "Commerce Building demolished",
            "The old Commerce Building at 200 Main St, Dallas, TX was demolished in 2025, "
            "Dallas County records confirm.",
            url="https://example.com/commerce-demo",
            published_at="2025-11-01",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.qc_status is QCStatus.EXCLUDE
    assert candidate.exclusions[0].rule == "demolished_structure"
    assert "demolished" in candidate.exclusions[0].evidence.lower()
    assert candidate.exclusions[0].source_url


# --- Test I: unknown structural specs route to Raphael/Onesimus ---


def test_i_structural_unknowns_not_excluded() -> None:
    ranking = _rank(
        _result(
            "Summit Tower listed",
            "Summit Tower at 200 Main St, Dallas, TX is listed for sale with an asking "
            "price of $5 million, Dallas County. No floor-load specifications published.",
            url="https://example.com/summit-listing",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.qc_status is not QCStatus.EXCLUDE
    assert any("Raphael" in step for step in candidate.next_steps)
    assert any("Onesimus" in step for step in candidate.next_steps)


# --- Test J: multi-building article, no attribution ---


def test_j_multi_building_no_attribution() -> None:
    ranking = _rank(
        _result(
            "Portfolio sale closes",
            "Alpha Tower in Dallas, TX and Beta Plaza in Dallas, TX sold for $20 million "
            "in a portfolio transaction.",
            url="https://example.com/portfolio-sale",
        )
    )
    assert len(ranking.candidates) == 1  # grouped once, flagged for review
    candidate = ranking.candidates[0]
    assert candidate.value_detail is None  # $20M unattributed
    assert ranking.value_order == []
    assert any("attribut" in note for note in candidate.unresolved)
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED


# --- Test K: fully occupied is evidence, not exclusion ---


def test_k_fully_occupied_not_excluded() -> None:
    ranking = _rank(
        _result(
            "Apex Tower fully leased",
            "Apex Tower reports 98% occupancy across the entire building, Dallas County.",
            url="https://example.com/apex-leased",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.qc_status is not QCStatus.EXCLUDE
    underuse_factor = next(f for f in candidate.opportunity.factors if f.factor == "underuse")
    assert underuse_factor.score is not None and underuse_factor.score < 0
    assert not any(s.signal == "high_vacancy" for s in candidate.underuse)


# --- Test L: downtown is secondary and municipality-neutral ---


def _downtown_pair():
    downtown = _result(
        "Alpha Tower listed",
        "Alpha Tower at 100 Main St, Dallas, TX is listed for sale with an asking "
        "price of $5 million in downtown Dallas, Dallas County.",
        url="https://example.com/alpha-downtown",
        published_at="2026-10-20",
    )
    plain = _result(
        "Beta Tower listed",
        "Beta Tower at 200 Main St, Dallas, TX is listed for sale with an asking "
        "price of $5 million, Dallas County.",
        url="https://example.com/beta-plain",
        published_at="2026-10-20",
    )
    return downtown, plain


def test_l_downtown_secondary_preference() -> None:
    ranking = _rank(*_downtown_pair())
    by_name = {candidate.name: candidate for candidate in ranking.candidates}
    assert by_name["Alpha Tower"].downtown is True
    assert by_name["Beta Tower"].downtown is False
    assert ranking.opportunity_order[0] == by_name["Alpha Tower"].key
    assert by_name["Alpha Tower"].opportunity.downtown_bonus == 0.05


def test_l_stronger_evidence_outweighs_downtown() -> None:
    downtown, _plain = _downtown_pair()
    distressed = _result(
        "Beta Tower distressed sale",
        "Beta Tower at 200 Main St, Dallas, TX is listed for sale with an asking "
        "price of $5 million after a bankruptcy filing, Dallas County.",
        url="https://example.com/beta-distress",
        published_at="2026-10-20",
    )
    ranking = _rank(downtown, distressed)
    by_name = {candidate.name: candidate for candidate in ranking.candidates}
    assert ranking.opportunity_order[0] == by_name["Beta Tower"].key


def test_l_downtown_not_dallas_exclusive() -> None:
    ranking = _rank(
        _result(
            "Irving Tower listed",
            "Irving Tower in downtown Irving is listed for sale, Dallas County.",
            url="https://example.com/irving-downtown",
        )
    )
    assert _only_candidate(ranking).downtown is True


# --- Test M: unknown versus negative evidence ---


def test_m_unknown_occupancy_not_zero() -> None:
    ranking = _rank(
        _result(
            "Alpha Tower listed",
            "Alpha Tower at 100 Main St, Dallas, TX is listed for sale, Dallas County.",
            url="https://example.com/alpha-noprice",
        ),
        _result(
            "Beta Tower leased",
            "Beta Tower at 200 Main St, Dallas, TX reports 95% occupancy across the "
            "entire building, Dallas County.",
            url="https://example.com/beta-leased",
        ),
    )
    by_name = {candidate.name: candidate for candidate in ranking.candidates}
    unknown_factor = next(f for f in by_name["Alpha Tower"].opportunity.factors if f.factor == "underuse")
    negative_factor = next(f for f in by_name["Beta Tower"].opportunity.factors if f.factor == "underuse")
    assert unknown_factor.score is None
    assert "unknown" in " ".join(unknown_factor.unknowns).lower()
    assert negative_factor.score is not None and negative_factor.score < 0


# --- Test N: repeated source, one candidate, no double counting ---


def test_n_repeated_source_single_candidate() -> None:
    first = _result(
        "Meridian Tower bankruptcy",
        "Meridian Tower at 500 Commerce St, Dallas, TX filed for bankruptcy, Dallas County.",
        url="https://example.com/meridian-news",
    )
    second = _result(
        "Meridian Tower bankruptcy",
        "Meridian Tower at 500 Commerce St, Dallas, TX filed for bankruptcy, Dallas County.",
        url="https://example.com/meridian-news",
    )
    ranking = _rank(first, second)
    candidate = _only_candidate(ranking)
    assert len(candidate.distress) == 2  # both references retained
    distress_factor = next(f for f in candidate.opportunity.factors if f.factor == "distress")
    assert distress_factor.score == 1.0  # signal counted once
    evidence_factor = next(f for f in candidate.opportunity.factors if f.factor == "evidence")
    assert "no independent corroboration" in evidence_factor.explanation


def test_n_distinct_domains_corroborate() -> None:
    first = _result(
        "Meridian Tower bankruptcy",
        "Meridian Tower at 500 Commerce St, Dallas, TX filed for bankruptcy.",
        url="https://example.com/meridian-news",
    )
    second = _result(
        "Meridian Tower filing",
        "Meridian Tower at 500 Commerce St, Dallas, TX bankruptcy filing confirmed.",
        url="https://other-press.example/meridian-filing",
    )
    candidate = _only_candidate(_rank(first, second))
    evidence_factor = next(f for f in candidate.opportunity.factors if f.factor == "evidence")
    assert "2 distinct source domains" in evidence_factor.explanation


# --- Test O: endpoint compatibility ---


def test_o_ranking_endpoint_additive(monkeypatch, tmp_path) -> None:
    import app.properties_db as store

    monkeypatch.setenv("OBS_ENABLED", "1")
    monkeypatch.setenv("OBS_OUTBOX_PATH", str(tmp_path / "outbox.sqlite3"))
    from app.db import DatabaseUnavailableError
    import app.observability.store as _event_store

    def _pg_down(config=None):
        raise DatabaseUnavailableError("pg unavailable")

    monkeypatch.setattr(_event_store, "get_connection", _pg_down)
    monkeypatch.setenv("RESEARCH_API_KEY", "unit-test-research-key")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "unit-test-brave-key")
    for var, value in (("DATABASE_HOST", "localhost"), ("DATABASE_PORT", "5432"),
                       ("DATABASE_NAME", "db"), ("DATABASE_USER", "u"),
                       ("DATABASE_PASSWORD", "pw")):
        monkeypatch.setenv(var, value)

    def _must_not_write(*args, **kwargs):
        raise AssertionError("ranking must not write properties")

    monkeypatch.setattr(store, "create_property", _must_not_write)
    httpx = pytest.importorskip("httpx")
    brave_calls: list = []

    def _get(url, params=None, headers=None, timeout=None):
        brave_calls.append(params["q"])

        class _Response:
            status_code = 200

            def json(self):
                return {"web": {"results": [
                    {"title": "Towers listed", "url": "https://example.com/t",
                     "description": "Meridian Tower offered for sale.", "age": "October 20, 2026"}
                ]}}

        return _Response()

    monkeypatch.setattr(httpx, "get", _get)
    spent = {"used": 0}

    def _reserve(n):
        spent["used"] += n
        return {"used": spent["used"], "ceiling": 100,
                "remaining": 100 - spent["used"], "month": "2026-10"}

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    from app.agents.gabriel.planner import plan_initial_searches

    assignments = [a.model_dump(mode="json") for a in plan_initial_searches()]
    headers = {"X-Research-Key": "unit-test-research-key"}
    # Legacy request shape still valid; intelligence on, ranking off by default.
    response = client.post("/discovery/research-runs", json={"assignments": assignments}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["intelligence"] is not None
    assert body["ranking"] is None
    # Ranking explicitly enabled rides the same results with no new Brave calls.
    before = len(brave_calls)
    ranked = client.post(
        "/discovery/research-runs",
        json={"assignments": assignments, "include_ranking": True},
        headers=headers,
    )
    assert ranked.status_code == 200
    assert ranked.json()["ranking"] is not None
    assert ranked.json()["ranking"]["ranking_version"] == "004B-5.0"
    assert len(brave_calls) == before + 3
    # Ranking without intelligence is rejected, not silently triggered.
    conflict = client.post(
        "/discovery/research-runs",
        json={"assignments": assignments, "include_intelligence": False, "include_ranking": True},
        headers=headers,
    )
    assert conflict.status_code == 422
    assert client.post("/discovery/research-runs", json={"assignments": assignments}).status_code == 401


# --- Test P: vacant parcel without a building ---


def test_p_vacant_parcel_excluded_without_invention() -> None:
    ranking = _rank(
        _result(
            "Industrial land for sale",
            "Vacant parcel at 900 Industrial Rd, Dallas County offered as land for sale.",
            url="https://example.com/parcel-900",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.qc_status is QCStatus.EXCLUDE
    assert candidate.exclusions[0].rule == "unsuitable_structure"
    text = (candidate.exclusions[0].evidence + candidate.exclusions[0].why_sufficient).lower()
    assert "zoning" not in text
    assert "structural" not in text
    assert "water" not in text and "electrical" not in text


# --- Scoring units ---


def test_value_ordering_and_unpriced_split() -> None:
    ranking = _rank(
        _result(
            "Alpha Tower listed",
            "Alpha Tower in Dallas, TX is listed for sale with an asking price of $5 million, Dallas County.",
            url="https://example.com/alpha-5m",
        ),
        _result(
            "Beta Tower listed",
            "Beta Tower in Dallas, TX is listed for sale with an asking price of $40 million, Dallas County.",
            url="https://example.com/beta-40m",
        ),
        _result(
            "Gamma Tower profile",
            "Gamma Tower in Dallas, TX operates near capacity, Dallas County.",
            url="https://example.com/gamma-profile",
        ),
    )
    assert [ranking.candidates[i].name for i in range(3)]
    by_name = {candidate.name: candidate for candidate in ranking.candidates}
    assert ranking.value_order == [by_name["Beta Tower"].key, by_name["Alpha Tower"].key]
    assert by_name["Beta Tower"].value_rank == 1
    assert ranking.unpriced == [by_name["Gamma Tower"].key]


def test_insufficient_data_flagged_not_ranked() -> None:
    ranking = _rank(
        _result(
            "Lonely Tower mentioned",
            "Lonely Tower was discussed as a Dallas landmark with no figures available.",
            url="https://example.com/lonely",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.opportunity.sufficient is False
    assert candidate.opportunity.score is None
    assert candidate.opportunity.completeness < 0.50
    assert ranking.insufficient_data == [candidate.key]
    assert ranking.opportunity_order == []
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED


def test_county_multiple_mentions_stay_unknown() -> None:
    ranking = _rank(
        _result(
            "Metro portfolio",
            "Summit Tower portfolio spans Dallas County and Tarrant County holdings.",
            url="https://example.com/metro-portfolio",
        )
    )
    candidate = _only_candidate(ranking)
    assert candidate.county.value == "county_unknown"
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED
