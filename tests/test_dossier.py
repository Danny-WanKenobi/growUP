"""Evidence dossier builder tests (5C): deterministic, no network, no database."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from pydantic import ValidationError

from app.agents.gabriel.dossier import (
    ClaimStatus,
    DossierRequest,
    build_dossiers,
)
from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
from app.agents.gabriel.ranking import (
    RANKING_VERSION,
    CountyEligibility,
    rank_intelligence,
)
from app.agents.gabriel.sources.brave import BraveSearchResult

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0


def _result(title, snippet, url="https://example.com/story", published_at=None, retrieved_at=None):
    global _RANK
    _RANK += 1
    return BraveSearchResult(
        assignment_id="gsp-dos001",
        query="test query dossier",
        title=title,
        url=url,
        snippet=snippet,
        domain=urlparse(url).netloc,
        provider="brave",
        retrieved_at=retrieved_at or _RETRIEVED,
        published_at=published_at,
        rank=_RANK,
    )


def _intel(*results):
    return ResearchIntelligence(results=[extract_intelligence(item) for item in results])


def _listing(price="$40 million", url="https://example.com/meridian", **kwargs):
    return _result(
        "Meridian Tower listed for sale",
        f"Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale with "
        f"an asking price of {price}. The Dallas County office tower drew bids.",
        url=url,
        **kwargs,
    )


def _topics(dossier):
    return [claim.topic for claim in dossier.supported_facts]


def test_one_dossier_per_property_across_rounds() -> None:
    round0 = _intel(_listing(published_at="2026-10-20"))
    round1 = _intel(
        _result(
            "Meridian Tower occupancy update",
            "Meridian Tower at 500 Commerce St, Dallas, TX reports 55% occupancy "
            "across the entire building.",
            url="https://example.com/meridian-update",
            published_at="2026-10-25",
        )
    )
    dossier_set = build_dossiers(
        DossierRequest(rounds=[round0, round1], executed_queries=['"Meridian Tower" occupancy'])
    )
    assert dossier_set.rounds_executed == 2
    assert dossier_set.total_results == 2
    assert len(dossier_set.dossiers) == 1
    dossier = dossier_set.dossiers[0]
    assert dossier.name == "Meridian Tower"
    assert dossier.street_address == "500 Commerce St"
    assert dossier.value_amount == "40000000"
    assert "asking_price" in _topics(dossier)
    assert "occupancy" in _topics(dossier)
    assert len(dossier.sources) == 2
    assert any("Round 0" in action for action in dossier.actions_performed)
    assert any("occupancy" in action for action in dossier.actions_performed)


def test_similar_names_stay_distinguishable() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(
                    _result(
                        "Meridian Tower listed",
                        "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale.",
                        url="https://example.com/tower",
                    ),
                    _result(
                        "Meridian Plaza listed",
                        "Meridian Plaza at 900 Elm St, Dallas, TX is listed for sale.",
                        url="https://example.com/plaza",
                    ),
                )
            ]
        )
    )
    assert sorted(d.key for d in dossier_set.dossiers) != []
    assert len(dossier_set.dossiers) == 2
    assert sorted(d.name for d in dossier_set.dossiers) == ["Meridian Plaza", "Meridian Tower"]


def test_multi_property_article_not_attributed() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(
                    _result(
                        "Portfolio sale closes",
                        "Alpha Tower and Beta Tower sold as a portfolio for $14.1 million, "
                        "the Dallas County deal closed quietly.",
                        url="https://example.com/portfolio",
                    )
                )
            ]
        )
    )
    assert len(dossier_set.dossiers) == 1
    dossier = dossier_set.dossiers[0]
    assert "asking_price" not in _topics(dossier)
    assert dossier.value_amount is None
    assert any("attribut" in text for text in dossier.missing)


def test_historical_transaction_stays_historical() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(
                    _result(
                        "Gateway Tower sale",
                        "Gateway Tower at 8111 LBJ Freeway, Dallas, TX sold for $14.1 million "
                        "in a completed deal.",
                        url="https://example.com/gateway",
                        published_at="2023-04-01",
                    )
                )
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    assert dossier.value_amount is None
    assert "asking_price" not in _topics(dossier)
    assert any(
        claim.topic == "transaction_history" and claim.status is ClaimStatus.HISTORICAL
        for claim in dossier.historical_observations
    )


def test_reported_occupancy_never_verified() -> None:
    dossier_set = build_dossiers(DossierRequest(rounds=[_intel(_listing())]))
    dossier = dossier_set.dossiers[0]
    for claim in dossier.supported_facts + dossier.historical_observations:
        assert claim.verification == "reported_unverified"


def test_availability_is_not_vacancy() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(
                    _result(
                        "Lease offering",
                        "Summit Tower at 200 Main St, Dallas, TX advertises 100,000 square "
                        "feet available for lease.",
                        url="https://example.com/summit-lease",
                    )
                )
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    assert "availability" in _topics(dossier)
    assert "occupancy" not in _topics(dossier)
    claim = next(c for c in dossier.supported_facts if c.topic == "availability")
    assert "NOT physical vacancy" in claim.statement


def test_distress_and_underuse_stay_separate() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(
                    _result(
                        "Distressed tower",
                        "Beacon Tower at 900 Elm St, Dallas, TX faces bankruptcy proceedings. "
                        "The Dallas County office tower operates at 35% occupancy across "
                        "the entire building.",
                        url="https://example.com/beacon",
                        published_at="2026-10-20",
                    )
                )
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    assert any(c.topic == "distress:bankruptcy" for c in dossier.distress_findings)
    assert all(c.topic.startswith("distress:") for c in dossier.distress_findings)
    assert all(c.topic.startswith("underuse:") or c.topic == "occupancy" for c in dossier.underuse_findings)
    assert dossier.underuse_findings or "occupancy" in _topics(dossier)


def test_independent_corroboration_supported() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(_listing(url="https://example.com/meridian")),
                _intel(
                    _result(
                        "Meridian Tower asking",
                        "Asking price for Meridian Tower at 500 Commerce St, Dallas, TX is "
                        "$40 million according to marketing materials.",
                        url="https://other.org/meridian-asking",
                        published_at="2026-10-21",
                    )
                ),
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    claim = next(c for c in dossier.supported_facts if c.topic == "asking_price")
    assert claim.status is ClaimStatus.SUPPORTED
    assert "2 distinct domains" in claim.corroboration_note


def test_identical_republication_not_corroboration() -> None:
    snippet = (
        "Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale with "
        "an asking price of $40 million."
    )
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(_result("Listing", snippet, url="https://example.com/a")),
                _intel(_result("Listing", snippet, url="https://other.org/b")),
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    claim = next(c for c in dossier.supported_facts if c.topic == "asking_price")
    assert claim.status is ClaimStatus.SINGLE_SOURCE
    assert "republication" in claim.corroboration_note


def test_same_period_price_conflict_reported() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(_listing(price="$25 million", published_at="2026-10-18")),
                _intel(
                    _listing(
                        price="$19 million",
                        url="https://other.org/meridian",
                        published_at="2026-10-22",
                    )
                ),
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    assert any(conflict.topic == "asking_price" for conflict in dossier.conflicts)
    assert any(
        claim.topic == "asking_price" and claim.status is ClaimStatus.CONTRADICTORY
        for claim in dossier.supported_facts
    )
    assert "conflict" in dossier.summary


def test_price_change_across_years_not_contradiction() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(_listing(price="$25 million", published_at="2024-05-01",
                                retrieved_at="2024-06-01T12:00:00+00:00")),
                _intel(
                    _listing(
                        price="$19 million",
                        url="https://other.org/meridian",
                        published_at="2026-09-01",
                        retrieved_at="2026-10-22T12:00:00+00:00",
                    )
                ),
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    assert dossier.conflicts == []
    assert any(
        "price change" in claim.statement for claim in dossier.historical_observations
    )
    current = next(c for c in dossier.supported_facts if c.topic == "asking_price")
    assert "19000000" in current.statement
    assert {ref.date_source for ref in current.evidence} == {"published"}


def test_occupancy_change_across_years_not_contradiction() -> None:
    def _occupancy(pct, published, retrieved, url):
        return _result(
            "Occupancy report",
            f"Meridian Tower at 500 Commerce St, Dallas, TX reports {pct}% occupancy "
            "across the entire building.",
            url=url,
            published_at=published,
            retrieved_at=retrieved,
        )

    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(_occupancy(35, "2024-05-01", "2024-06-01T12:00:00+00:00",
                                  "https://example.com/old")),
                _intel(_occupancy(60, "2026-09-01", "2026-10-22T12:00:00+00:00",
                                  "https://other.org/new")),
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    assert dossier.conflicts == []
    occupancy = [c for c in dossier.supported_facts if c.topic == "occupancy"]
    assert len(occupancy) == 2
    assert {ref.date_source for claim in occupancy for ref in claim.evidence} == {"published"}


def test_reassessment_reuses_ranking_engine() -> None:
    round0 = _intel(_listing())
    dossier_set = build_dossiers(DossierRequest(rounds=[round0]))
    merged = ResearchIntelligence(results=list(round0.results))
    assert dossier_set.ranking == rank_intelligence(merged)
    assert dossier_set.ranking.ranking_version == RANKING_VERSION


def test_unknown_stays_unknown() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(
                    _result(
                        "Tower mentioned",
                        "Meridian Tower in Dallas, TX was mentioned in a market roundup.",
                        url="https://example.com/roundup",
                    )
                )
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    assert dossier.value_amount is None
    assert dossier.opportunity_sufficient is False
    assert dossier.missing
    assert "No asking price documented." in dossier.summary
    assert "$" not in dossier.summary


def test_handoffs_route_to_correct_agents() -> None:
    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                _intel(
                    _result(
                        "Tower profile",
                        "Meridian Tower in Dallas, TX operates as offices.",
                        url="https://example.com/profile",
                    )
                )
            ]
        )
    )
    dossier = dossier_set.dossiers[0]
    agents = {handoff.agent for handoff in dossier.handoffs}
    assert agents <= {"Gabriel", "Raphael", "Onesimus", "Priscilla"}
    assert "Raphael" in agents  # county and occupancy verification
    assert dossier.remaining_questions


def test_summary_contains_no_fabricated_details() -> None:
    dossier_set = build_dossiers(DossierRequest(rounds=[_intel(_listing())]))
    dossier = dossier_set.dossiers[0]
    assert "Meridian Tower" in dossier.summary
    assert "Dallas County" in dossier.summary
    assert "verified" not in dossier.summary.lower()
    assert "%" not in dossier.summary  # no occupancy was reported


def test_comparison_explains_new_evidence() -> None:
    round0 = _intel(
        _result(
            "Tower profile",
            "Meridian Tower at 500 Commerce St, Dallas, TX operates as offices. "
            "The Dallas County tower is well known.",
            url="https://example.com/profile",
        )
    )
    prior = rank_intelligence(ResearchIntelligence(results=list(round0.results)))
    round1 = _intel(_listing(published_at="2026-10-20"))
    dossier_set = build_dossiers(DossierRequest(rounds=[round0, round1], prior_ranking=prior))
    assert len(dossier_set.comparison) == 1
    entry = dossier_set.comparison[0]
    assert any("asking price newly attributed" in note for note in entry.notes)
    assert any("expanded" in note for note in entry.notes)


def test_no_comparison_without_prior_ranking() -> None:
    dossier_set = build_dossiers(DossierRequest(rounds=[_intel(_listing())]))
    assert dossier_set.comparison == []


def test_dossiers_endpoint_returns_dossiers(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("OBS_ENABLED", "1")
    monkeypatch.setenv("OBS_OUTBOX_PATH", str(tmp_path / "outbox.sqlite3"))
    from app.db import DatabaseUnavailableError
    import app.observability.store as _event_store

    def _pg_down(config=None):
        raise DatabaseUnavailableError("pg unavailable")

    monkeypatch.setattr(_event_store, "get_connection", _pg_down)
    client = TestClient(app, raise_server_exceptions=False)
    intel = _intel(_listing())
    response = client.post(
        "/discovery/dossiers",
        json={"rounds": [intel.model_dump(mode="json")]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dossier_version"] == "004B-5C.0"
    assert len(body["dossiers"]) == 1
    assert body["ranking"]["ranking_version"] == RANKING_VERSION


def test_dossiers_endpoint_rejects_empty_rounds() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/discovery/dossiers", json={"rounds": []}).status_code == 422


def test_dossier_deterministic() -> None:
    request = DossierRequest(rounds=[_intel(_listing(published_at="2026-10-20"))])
    assert build_dossiers(request) == build_dossiers(request)


def test_round_and_result_bounds_rejected() -> None:
    from app.agents.gabriel.extractor import ResultIntelligence

    with pytest.raises(ValidationError):
        DossierRequest(rounds=[_intel(_listing())] * 11)
    many = ResearchIntelligence(
        results=[
            ResultIntelligence(
                assignment_id="x", query="y", result_url=f"https://example.com/{n}",
                domain="example.com", title="t",
            )
            for n in range(201)
        ]
    )
    with pytest.raises(ValidationError):
        DossierRequest(rounds=[many])
