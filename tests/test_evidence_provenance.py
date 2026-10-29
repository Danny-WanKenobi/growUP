"""Research evidence provenance tests (G5-4 financial chronology, G5-5 source roles).

Retrieval timestamps must never stand in for publication dates in
financial chronology, and dossier/ranking source lists must distinguish
supporting, contradictory, and unattributed member URLs.
Deterministic; no network, no database.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.agents.gabriel.dossier import (
    ClaimStatus,
    DossierRequest,
    build_dossiers,
)
from app.agents.gabriel.extractor import (
    ResearchIntelligence,
    extract_intelligence,
)
from app.agents.gabriel.ranking import QCStatus, rank_intelligence
from app.agents.gabriel.sources.brave import BraveSearchResult

_RETRIEVED = "2026-10-27T12:00:00+00:00"
_RANK = 0
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


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


def _dossier(*results):
    dossier_set = build_dossiers(DossierRequest(rounds=[_intel(*results)]))
    assert len(dossier_set.dossiers) == 1
    return dossier_set.dossiers[0]


def _asking_statements(dossier):
    return [c.statement for c in dossier.supported_facts if c.topic == "asking_price"]


# --- G5-4: financial chronology ---


def test_undated_asking_plus_dated_asking_no_invented_change() -> None:
    dated = _listing("$25 million", published_at="2023-05-01",
                     retrieved_at="2023-06-01T12:00:00+00:00")
    undated = _listing("$40 million", url="https://other.org/meridian",
                       published_at=None, retrieved_at="2024-06-01T12:00:00+00:00")
    intel = _intel(dated, undated)
    candidate = rank_intelligence(intel).candidates[0]
    assert candidate.value_detail is None  # contested suppression intact
    dossier = _dossier(dated, undated)
    statements = _asking_statements(dossier)
    assert statements
    assert not any("price change" in text for text in statements)
    assert not any(_YEAR_RE.search(text) for text in statements)
    conflicts = [c for c in dossier.conflicts if c.topic == "asking_price"]
    assert len(conflicts) == 1
    assert not _YEAR_RE.search(conflicts[0].description)


def test_multiple_undated_askings_no_year_claims() -> None:
    first = _listing("$25 million", retrieved_at="2023-06-01T12:00:00+00:00")
    second = _listing("$40 million", url="https://other.org/meridian",
                      retrieved_at="2025-06-01T12:00:00+00:00")
    dossier = _dossier(first, second)
    statements = _asking_statements(dossier)
    assert statements
    assert all(
        c.status is ClaimStatus.CONTRADICTORY
        for c in dossier.supported_facts if c.topic == "asking_price"
    )
    assert not any(_YEAR_RE.search(text) for text in statements)
    conflicts = [c for c in dossier.conflicts if c.topic == "asking_price"]
    assert len(conflicts) == 1
    assert not _YEAR_RE.search(conflicts[0].description)


def test_undated_occupancy_plus_dated_occupancy_conflict_kept() -> None:
    dated = _result(
        "Tower occupancy report",
        "Meridian Tower at 500 Commerce St, Dallas, TX reports occupancy of 85% "
        "across the entire building. Dallas County.",
        url="https://example.com/occ-old",
        published_at="2023-05-01",
        retrieved_at="2023-06-01T12:00:00+00:00",
    )
    undated = _result(
        "Tower occupancy update",
        "Meridian Tower at 500 Commerce St, Dallas, TX reports occupancy of 40% "
        "across the entire building. Dallas County.",
        url="https://example.com/occ-new",
        published_at=None,
        retrieved_at="2024-06-01T12:00:00+00:00",
    )
    dossier = _dossier(dated, undated)
    conflicts = [c for c in dossier.conflicts if c.topic == "occupancy"]
    assert len(conflicts) == 1  # retrieval-year spread must not suppress it
    assert not _YEAR_RE.search(conflicts[0].description)


def test_two_published_years_preserve_change_narrative() -> None:
    old = _listing("$25 million", published_at="2023-05-01",
                   retrieved_at="2023-06-01T12:00:00+00:00")
    new = _listing("$40 million", url="https://other.org/meridian",
                   published_at="2024-05-01", retrieved_at="2024-06-01T12:00:00+00:00")
    dossier = _dossier(old, new)
    history = [c.statement for c in dossier.historical_observations
               if c.topic == "asking_price"]
    assert any("price change" in text for text in history)
    current = [c for c in dossier.supported_facts if c.topic == "asking_price"]
    assert len(current) == 1
    assert "40000000" in current[0].statement


def test_claim_evidence_carries_date_source() -> None:
    dated = _listing("$25 million", published_at="2023-05-01",
                     retrieved_at="2023-06-01T12:00:00+00:00")
    undated = _listing("$40 million", url="https://other.org/meridian",
                       published_at=None, retrieved_at="2024-06-01T12:00:00+00:00")
    dateless = _listing("$30 million", url="https://third.org/meridian",
                        published_at=None, retrieved_at="")
    dossier = _dossier(dated, undated, dateless)
    refs = [ref for claim in dossier.supported_facts for ref in claim.evidence]
    refs += [ref for conflict in dossier.conflicts for ref in conflict.evidence]
    by_url = {}
    for ref in refs:
        by_url.setdefault(ref.url, ref.date_source)
    assert by_url["https://example.com/meridian"] == "published"
    assert by_url["https://other.org/meridian"] == "retrieved"
    assert by_url["https://third.org/meridian"] == "unknown"


def test_claim_evidence_date_strings_unchanged() -> None:
    dated = _listing("$25 million", published_at="2023-05-01",
                     retrieved_at="2023-06-01T12:00:00+00:00")
    undated = _listing("$40 million", url="https://other.org/meridian",
                       published_at=None, retrieved_at="2024-06-01T12:00:00+00:00")
    dateless = _listing("$30 million", url="https://third.org/meridian",
                        published_at=None, retrieved_at="")
    dossier = _dossier(dated, undated, dateless)
    refs = [ref for claim in dossier.supported_facts for ref in claim.evidence]
    by_url = {}
    for ref in refs:
        by_url.setdefault(ref.url, ref.date)
    assert by_url["https://example.com/meridian"] == "2023-05-01"
    assert by_url["https://other.org/meridian"] == "2024-06-01T12:00:00+00:00"
    assert by_url["https://third.org/meridian"] is None


def test_undated_asking_never_dated_by_retrieval_year() -> None:
    dated = _listing("$25 million", published_at="2026-09-01",
                     retrieved_at="2026-10-01T12:00:00+00:00")
    undated = _listing("$40 million", url="https://other.org/meridian",
                       published_at=None, retrieved_at="2025-06-01T12:00:00+00:00")
    dossier = _dossier(dated, undated)
    statements = _asking_statements(dossier)
    assert not any("price change" in text for text in statements)
    assert not any(_YEAR_RE.search(text) for text in statements)
    conflicts = [c for c in dossier.conflicts if c.topic == "asking_price"]
    assert len(conflicts) == 1
    assert not _YEAR_RE.search(conflicts[0].description)


def test_date_source_present_on_all_claim_evidence() -> None:
    item = _listing("$40 million", published_at="2026-10-20")
    dossier = _dossier(item)
    sections = (
        dossier.supported_facts + dossier.historical_observations
        + dossier.distress_findings + dossier.underuse_findings
    )
    refs = [ref for claim in sections for ref in claim.evidence]
    refs += [ref for conflict in dossier.conflicts for ref in conflict.evidence]
    assert refs
    assert all(ref.date_source in ("published", "retrieved", "unknown") for ref in refs)


# --- G5-5: conservative source roles ---


def _roles(candidate_or_dossier):
    return candidate_or_dossier.source_roles


def test_supporting_source_role() -> None:
    url = "https://example.com/meridian"
    intel = _intel(_listing("$40 million", published_at="2026-10-20"))
    ranking = rank_intelligence(intel)
    assert len(ranking.candidates) == 1
    candidate = ranking.candidates[0]
    assert candidate.sources == [url]
    assert _roles(candidate)[url] == ["claim_evidence"]
    dossier = build_dossiers(DossierRequest(rounds=[intel])).dossiers[0]
    assert dossier.sources == [url]
    assert _roles(dossier)[url] == ["claim_evidence"]


def test_contradictory_source_role() -> None:
    first = _result(
        "Alpha Tower profile",
        "Alpha Tower at 100 Main St, Dallas, TX holds offices. Dallas County.",
        url="https://example.com/alpha",
    )
    second = _result(
        "Alpha Tower profile",
        "Alpha Tower at 100 Main St, Dallas, TX holds offices. Harris County.",
        url="https://other.org/alpha",
    )
    intel = _intel(first, second)
    ranking = rank_intelligence(intel)
    assert len(ranking.candidates) == 1
    candidate = ranking.candidates[0]
    assert candidate.sources == ["https://example.com/alpha", "https://other.org/alpha"]
    # The first member also surfaces building-factor evidence, so both
    # roles apply; the second member links only through the contradiction.
    assert _roles(candidate)["https://example.com/alpha"] == [
        "claim_evidence", "contradictory_evidence"]
    assert _roles(candidate)["https://other.org/alpha"] == ["contradictory_evidence"]
    dossier = build_dossiers(DossierRequest(rounds=[intel])).dossiers[0]
    assert sorted(_roles(dossier)["https://example.com/alpha"]) == [
        "claim_evidence", "contradictory_evidence"]


def test_unattributed_member_role() -> None:
    listed = _listing("$40 million", published_at="2026-10-20")
    portfolio = _result(
        "Two towers change hands",
        "Meridian Tower at 500 Commerce St, Dallas, TX and Bayou Tower at "
        "500 Smith St, Houston, TX sold in one portfolio deal for $90 million.",
        url="https://example.com/portfolio",
    )
    intel = _intel(listed, portfolio)
    assert len(intel.results[1].properties) > 1
    ranking = rank_intelligence(intel)
    candidate = next(c for c in ranking.candidates if c.key.startswith("addr:500 commerce"))
    assert candidate.sources == [
        "https://example.com/meridian", "https://example.com/portfolio"]
    assert _roles(candidate)["https://example.com/meridian"] == ["claim_evidence"]
    assert _roles(candidate)["https://example.com/portfolio"] == ["member_only"]
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    dossier = next(d for d in dossier_set.dossiers if d.key == candidate.key)
    assert _roles(dossier)["https://example.com/portfolio"] == ["member_only"]


def test_source_with_multiple_roles() -> None:
    first = _listing("$25 million", published_at="2026-10-18")
    second = _listing("$40 million", url="https://other.org/meridian",
                      published_at="2026-10-22")
    intel = _intel(first, second)
    candidate = rank_intelligence(intel).candidates[0]
    assert candidate.value_detail is None
    assert _roles(candidate)["https://example.com/meridian"] == [
        "claim_evidence", "contradictory_evidence"]
    assert _roles(candidate)["https://other.org/meridian"] == [
        "claim_evidence", "contradictory_evidence"]
    dossier = build_dossiers(DossierRequest(rounds=[intel])).dossiers[0]
    assert _roles(dossier)["https://example.com/meridian"] == [
        "claim_evidence", "contradictory_evidence"]


def test_shared_url_roles_are_candidate_specific() -> None:
    shared = "https://example.com/shared"
    portfolio = _result(
        "Two halls change hands",
        "Alpha Tower at 100 Main St, Dallas, TX and Beta Hall at "
        "300 Elm St, Dallas, TX sold in one portfolio deal for $90 million.",
        url=shared,
    )
    priced = _result(
        "Beta Tower listed for sale",
        "Beta Tower at 200 Main St, Dallas, TX is listed for sale "
        "with an asking price of $40 million. Dallas County.",
        url=shared,
        published_at="2026-10-20",
    )
    intel = _intel(portfolio, priced)
    assert len(intel.results[0].properties) > 1
    ranking = rank_intelligence(intel)
    plain = next(c for c in ranking.candidates if "100 main" in c.key)
    rich = next(c for c in ranking.candidates if "200 main" in c.key)
    assert shared in plain.sources
    assert rich.sources == [shared]
    assert _roles(plain)[shared] == ["member_only"]
    assert _roles(rich)[shared] == ["claim_evidence"]


def test_roles_with_disputed_identity() -> None:
    first = _result(
        "Alpha Tower",
        "Alpha Tower at 100 Main St, Dallas, TX is listed.",
        url="https://example.com/a",
    )
    second = _result(
        "Beta Tower",
        "Beta Tower at 100 Main St, Dallas, TX is listed.",
        url="https://example.com/b",
    )
    intel = _intel(first, second)
    candidate = rank_intelligence(intel).candidates[0]
    assert candidate.identity_disputes
    assert set(_roles(candidate)) == {"https://example.com/a", "https://example.com/b"}
    assert rank_intelligence(intel).candidates[0].source_roles == _roles(candidate)


def test_sources_unchanged_by_roles() -> None:
    listed = _listing("$40 million", published_at="2026-10-20")
    portfolio = _result(
        "Two towers change hands",
        "Meridian Tower at 500 Commerce St, Dallas, TX and Bayou Tower at "
        "500 Smith St, Houston, TX sold in one portfolio deal for $90 million.",
        url="https://example.com/portfolio",
    )
    intel = _intel(listed, portfolio)
    candidate = next(
        c for c in rank_intelligence(intel).candidates if c.key.startswith("addr:500 commerce"))
    assert candidate.sources == [
        "https://example.com/meridian", "https://example.com/portfolio"]
    dossier = next(
        d for d in build_dossiers(DossierRequest(rounds=[intel])).dossiers
        if d.key == candidate.key)
    assert dossier.sources == [
        "https://example.com/meridian", "https://example.com/portfolio"]


def test_roles_do_not_change_scoring() -> None:
    intel = _intel(_listing("$40 million", published_at="2026-10-20"))
    candidate = rank_intelligence(intel).candidates[0]
    assert candidate.qc_status is QCStatus.PASS
    assert candidate.value_detail is not None
    assert candidate.value_detail.amount == "40000000"
    assert candidate.opportunity.sufficient is True
    assert candidate.source_roles  # roles observe; they do not score
