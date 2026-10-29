"""Contested asking prices, ranges, and evidence-score tests (G3-3/G3-4).

A contested asking price must never headline as authoritative: both
observations stay available with their sources, the contradiction stays
documented, and the candidate leaves value ordering until reconciled.
Corroboration credit requires attributable facts, not mere membership.
Deterministic; no network, no database.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlparse

from app.agents.gabriel.dossier import (
    ClaimStatus,
    DossierRequest,
    build_dossiers,
)
from app.agents.gabriel.extractor import (
    MoneyKind,
    ResearchIntelligence,
    extract_intelligence,
    extract_money,
)
from app.agents.gabriel.investigation import plan_investigations
from app.agents.gabriel.ranking import QCStatus, rank_intelligence
from app.agents.gabriel.sources.brave import BraveSearchResult

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0


def _result(title, snippet, url="https://example.com/story", published_at=None,
            retrieved_at=_RETRIEVED):
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
        retrieved_at=retrieved_at,
        published_at=published_at,
        rank=_RANK,
    )


def _intel(*results):
    return ResearchIntelligence(results=[extract_intelligence(item) for item in results])


def _rank(*results):
    return rank_intelligence(_intel(*results))


def _only_candidate(ranking):
    assert len(ranking.candidates) == 1
    return ranking.candidates[0]


def _factor(candidate, name):
    return next(f for f in candidate.opportunity.factors if f.factor == name)


def _listing(price, url="https://example.com/meridian", published_at=None,
             retrieved_at=_RETRIEVED):
    return _result(
        "Meridian Tower listed for sale",
        f"Meridian Tower at 500 Commerce St, Dallas, TX is listed for sale "
        f"with an asking price of {price}. Dallas County.",
        url=url,
        published_at=published_at,
        retrieved_at=retrieved_at,
    )


# --- G3-3: contested asking prices ---


def test_1_conflicting_current_askings_suppressed() -> None:
    ranking = _rank(
        _listing("$25 million", published_at="2026-10-18"),
        _listing("$40 million", url="https://other.org/meridian", published_at="2026-10-22"),
    )
    candidate = _only_candidate(ranking)
    assert any("conflicting asking prices" in note for note in candidate.unresolved)
    assert candidate.value_detail is None
    assert candidate.key in ranking.unpriced
    assert ranking.value_order == []
    value = _factor(candidate, "value")
    assert value.score is None
    assert len(value.evidence) == 2
    assert value.unknowns
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED


def test_2_historical_price_change_ranks_conservative_dossier_narrates() -> None:
    retrieved = "2026-01-05T12:00:00+00:00"
    old = _listing("$25 million", published_at="2025-12-20", retrieved_at=retrieved)
    new = _listing("$19 million", url="https://other.org/meridian",
                   published_at="2026-01-02", retrieved_at=retrieved)
    ranking = rank_intelligence(_intel(old, new))
    candidate = _only_candidate(ranking)
    # Ranking stays year-blind and conservative: contradiction flagged, no headline.
    assert any("conflicting asking prices" in note for note in candidate.unresolved)
    assert candidate.value_detail is None
    dossier_set = build_dossiers(
        DossierRequest(rounds=[_intel(old), _intel(new)])
    )
    (dossier,) = dossier_set.dossiers
    assert dossier.conflicts == []
    assert any("price change" in claim.statement for claim in dossier.historical_observations)
    current = next(c for c in dossier.supported_facts if c.topic == "asking_price")
    assert "19000000" in current.statement
    assert dossier.value_amount is None
    assert "contested or reported as a range" in dossier.summary


def test_3_equivalent_askings_keep_headline() -> None:
    ranking = _rank(
        _listing("$40 million", published_at="2026-10-18"),
        _listing("$40 million", url="https://other.org/meridian", published_at="2026-10-22"),
    )
    candidate = _only_candidate(ranking)
    assert not any(note.startswith("contradiction:") for note in candidate.unresolved)
    assert candidate.value_detail is not None
    assert candidate.value_detail.amount == "40000000"
    assert _factor(candidate, "value").score == 1.0
    assert ranking.value_order == [candidate.key]


def test_4_measurement_types_never_headline() -> None:
    estimated = _only_candidate(_rank(_result(
        "Meridian Tower valuation",
        "Meridian Tower at 500 Commerce St, Dallas, TX, valued at $8 million, drew bids.",
        url="https://example.com/value",
    )))
    assert estimated.value_detail is None
    assert _factor(estimated, "value").score == 0.1
    historical = _only_candidate(_rank(_result(
        "Meridian Tower history",
        "Meridian Tower at 500 Commerce St, Dallas, TX: the asking price was $3 million in 2021.",
        url="https://example.com/history",
    )))
    assert historical.value_detail is None
    assert _factor(historical, "value").score == 0.1
    completed = _only_candidate(_rank(_result(
        "Meridian Tower sold",
        "Meridian Tower at 500 Commerce St, Dallas, TX sold the site for $1.5M last year.",
        url="https://example.com/sold",
    )))
    assert completed.value_detail is None
    assert _factor(completed, "value").score == 0.1


def test_5_portfolio_price_stays_unattributed() -> None:
    ranking = _rank(_result(
        "Portfolio asking",
        "Asking price $50 million for the portfolio: 500 Commerce St and "
        "900 Industrial Blvd, Dallas, TX.",
        url="https://example.com/portfolio",
    ))
    assert len(ranking.candidates) == 2
    for candidate in ranking.candidates:
        assert candidate.value_detail is None
        assert any("cannot be safely attributed" in note for note in candidate.unresolved)
        assert not any(note.startswith("contradiction:") for note in candidate.unresolved)
        assert candidate.key in ranking.unpriced
    assert ranking.value_order == []


def test_6_single_price_control_unchanged() -> None:
    ranking = _rank(_listing("$40 million", published_at="2026-10-20"))
    candidate = _only_candidate(ranking)
    assert candidate.qc_status is QCStatus.PASS
    assert candidate.value_detail is not None
    assert candidate.value_detail.amount == "40000000"
    assert "example.com/meridian" in candidate.value_detail.url
    assert _factor(candidate, "value").score == 1.0
    assert candidate.value_rank == 1
    assert ranking.value_order == [candidate.key]


# --- Price ranges ---


def test_7_unitless_first_endpoint_scales() -> None:
    (mention,) = extract_money("Meridian Tower asking $5-7 million for the block.")
    assert mention.amount == Decimal("5000000")
    assert mention.amount_high == Decimal("7000000")
    assert mention.kind is MoneyKind.ASKING_PRICE_CURRENT
    assert "$5-7 million" in mention.evidence


def test_8_range_syntax_variants() -> None:
    cases = [
        "asking $5-7 million",  # hyphen-minus
        "asking $5–7 million",  # en dash
        "asking $5—7 million",  # em dash
        "asking $5 to $7 million",
        "asking $5 million-$7 million",
    ]
    for text in cases:
        mentions = extract_money(f"Meridian Tower {text} downtown.")
        assert len(mentions) == 1, text
        (mention,) = mentions
        assert (mention.amount, mention.amount_high) == (Decimal("5000000"), Decimal("7000000")), text


def test_9_price_reduction_never_a_range() -> None:
    first = extract_money("Tower reduced from $4 million to $3.5 million.")
    assert len(first) == 2
    assert all(m.amount_high is None for m in first)
    assert first[0].kind is MoneyKind.ASKING_PRICE_HISTORICAL
    second = extract_money("Tower reduced from $4 to $3.5 million.")
    assert len(second) == 2
    assert all(m.amount_high is None for m in second)
    assert all(m.kind is MoneyKind.ASKING_PRICE_HISTORICAL for m in second)
    ranking = _rank(_result(
        "Tower reduction",
        "Meridian Tower at 500 Commerce St, Dallas, TX reduced from $4 to $3.5 million.",
        url="https://example.com/reduction",
    ))
    candidate = _only_candidate(ranking)
    assert candidate.value_detail is None
    assert not any("range" in note for note in candidate.unresolved)


def test_10_between_and_range_supported() -> None:
    # "Between X and Y <unit>" is unambiguous: one ranged observation.
    (mention,) = extract_money("Meridian Tower asking between $5 and $7 million.")
    assert (mention.amount, mention.amount_high) == (Decimal("5000000"), Decimal("7000000"))
    assert mention.kind is MoneyKind.ASKING_PRICE_CURRENT


def test_11_range_ranking_never_headlines() -> None:
    ranking = _rank(_result(
        "Meridian Tower range",
        "Meridian Tower at 500 Commerce St, Dallas, TX asking price of $5-7 million.",
        url="https://example.com/range",
        published_at="2026-10-20",
    ))
    candidate = _only_candidate(ranking)
    assert candidate.value_detail is None
    assert any("range" in note for note in candidate.unresolved)
    assert not any(note.startswith("contradiction:") for note in candidate.unresolved)
    assert _factor(candidate, "value").score is None
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED
    assert candidate.key in ranking.unpriced


def test_12_range_dossier_marks_review() -> None:
    intel = _intel(_result(
        "Meridian Tower range",
        "Meridian Tower at 500 Commerce St, Dallas, TX asking price of $5-7 million.",
        url="https://example.com/range",
        published_at="2026-10-20",
    ))
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    (dossier,) = dossier_set.dossiers
    assert not [c for c in dossier.supported_facts if c.topic == "asking_price"]
    assert not [c for c in dossier.conflicts if c.topic == "asking_price"]
    assert any("range" in note for note in dossier.missing)
    assert dossier.value_amount is None
    assert "contested or reported as a range" in dossier.summary


def test_dossier_newest_year_disagreement_flagged() -> None:
    dossier_set = build_dossiers(
        DossierRequest(rounds=[
            _intel(_listing("$25 million", published_at="2023-06-01T12:00:00+00:00",
                             retrieved_at="2023-07-01T12:00:00+00:00")),
            _intel(_listing("$19 million", url="https://other.org/meridian",
                             published_at="2024-03-01T12:00:00+00:00",
                             retrieved_at="2024-04-01T12:00:00+00:00")),
            _intel(_listing("$15 million", url="https://third.net/meridian",
                             published_at="2024-09-01T12:00:00+00:00",
                             retrieved_at="2024-10-01T12:00:00+00:00")),
        ])
    )
    (dossier,) = dossier_set.dossiers
    assert any(conflict.topic == "asking_price" for conflict in dossier.conflicts)
    asking = [c for c in dossier.supported_facts if c.topic == "asking_price"]
    assert asking
    assert all(c.status is ClaimStatus.CONTRADICTORY for c in asking)
    assert not any("Current asking price" in c.statement for c in asking)
    assert any("price change" in claim.statement or "2023" in claim.statement
               for claim in dossier.historical_observations)


def test_dossier_summary_clean_price_uncontested() -> None:
    dossier_set = build_dossiers(DossierRequest(rounds=[_intel(_listing("$40 million"))]))
    (dossier,) = dossier_set.dossiers
    assert dossier.value_amount == "40000000"
    assert "contested" not in dossier.summary
    assert "Asking price 40000000" in dossier.summary


# --- G3-4: evidence credit requires attributable facts ---


def test_13_portfolio_member_earns_no_corroboration() -> None:
    ranking = _rank(
        _result(
            "Meridian Tower bankruptcy",
            "Meridian Tower at 500 Commerce St, Dallas, TX filed for bankruptcy.",
            url="https://example.com/meridian-news",
        ),
        _result(
            "Portfolio asking",
            "Asking price $50 million for the portfolio: 500 Commerce St and "
            "900 Industrial Blvd, Dallas, TX.",
            url="https://other.org/portfolio",
        ),
    )
    candidate = next(c for c in ranking.candidates if "500 commerce" in c.key)
    evidence = _factor(candidate, "evidence")
    assert "single source domain" in evidence.explanation
    assert evidence.score == 0.2
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED
    assert len(candidate.sources) == 2  # membership preserved


def test_14_two_attributing_domains_keep_credit() -> None:
    ranking = _rank(
        _result(
            "Meridian Tower bankruptcy",
            "Meridian Tower at 500 Commerce St, Dallas, TX filed for bankruptcy.",
            url="https://example.com/meridian-news",
        ),
        _result(
            "Meridian Tower filing",
            "Meridian Tower at 500 Commerce St, Dallas, TX bankruptcy filing confirmed.",
            url="https://other-press.example/meridian-filing",
        ),
    )
    candidate = _only_candidate(ranking)
    evidence = _factor(candidate, "evidence")
    assert "2 distinct source domains" in evidence.explanation
    assert evidence.score == 0.6


def test_15_identity_only_source_earns_no_credit() -> None:
    ranking = _rank(
        _result(
            "Meridian Tower bankruptcy",
            "Meridian Tower at 500 Commerce St, Dallas, TX filed for bankruptcy.",
            url="https://example.com/meridian-news",
        ),
        _result(
            "Meridian Tower for sale",
            "Meridian Tower at 500 Commerce St, Dallas, TX is offered for sale.",
            url="https://other.org/meridian-listing",
            published_at="2026-10-20",
        ),
    )
    candidate = _only_candidate(ranking)
    evidence = _factor(candidate, "evidence")
    assert "single source domain" in evidence.explanation
    assert "recent evidence present" not in evidence.explanation
    assert evidence.score == 0.2
    assert candidate.name == "Meridian Tower"  # identity preserved
    assert len(candidate.sources) == 2


def test_16_unattributed_recent_source_earns_no_recency() -> None:
    ranking = _rank(_result(
        "Portfolio for sale",
        "Portfolio properties for sale: 500 Commerce St and 900 Industrial Blvd, Dallas, TX.",
        url="https://example.com/portfolio",
        published_at="2026-10-20",
    ))
    candidate = next(c for c in ranking.candidates if "500 commerce" in c.key)
    evidence = _factor(candidate, "evidence")
    assert "recent evidence present" not in evidence.explanation
    assert evidence.score == 0.2
    assert candidate.qc_status is QCStatus.RESEARCH_REQUIRED


def test_17_same_domain_repetition_not_independent() -> None:
    snippet = "Meridian Tower at 500 Commerce St, Dallas, TX filed for bankruptcy."
    ranking = _rank(
        _result("Meridian Tower bankruptcy", snippet, url="https://example.com/meridian-news"),
        _result("Meridian Tower bankruptcy", snippet, url="https://example.com/meridian-news"),
    )
    candidate = _only_candidate(ranking)
    evidence = _factor(candidate, "evidence")
    assert "single source domain" in evidence.explanation
    assert evidence.score == 0.2


def test_18_ranking_investigation_dossier_deterministic() -> None:
    intel = _intel(
        _listing("$25 million", published_at="2026-10-18"),
        _listing("$40 million", url="https://other.org/meridian", published_at="2026-10-22"),
    )
    first_ranking = rank_intelligence(intel)
    second_ranking = rank_intelligence(intel)
    assert first_ranking == second_ranking
    first_plan = plan_investigations(intel, ranking=first_ranking)
    second_plan = plan_investigations(intel, ranking=second_ranking)
    assert first_plan == second_plan
    rounds = [intel]
    assert build_dossiers(DossierRequest(rounds=rounds)) == build_dossiers(DossierRequest(rounds=rounds))
