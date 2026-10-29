"""Canonical source-host identity tests (B5-2A step 2A.1).

Deterministic; no network, no database, no provider calls.
"""

from __future__ import annotations

from urllib.parse import urlparse

from app.agents.gabriel.source_identity import (
    canonical_host_text,
    canonical_hostname,
    evidence_host,
)


def test_case_differences_unify() -> None:
    assert canonical_hostname("https://News.Example.COM/story") == "news.example.com"
    assert canonical_hostname("HTTPS://EXAMPLE.COM/") == "example.com"


def test_default_ports_dropped() -> None:
    assert canonical_hostname("http://example.com:80/x") == "example.com"
    assert canonical_hostname("https://example.com:443/x") == "example.com"


def test_nondefault_ports_preserved() -> None:
    assert canonical_hostname("https://example.com:8443/x") == "example.com:8443"
    assert canonical_hostname("http://example.com:8080/") == "example.com:8080"


def test_trailing_dots_stripped() -> None:
    assert canonical_hostname("https://example.com./x") == "example.com"


def test_userinfo_removed() -> None:
    assert canonical_hostname("https://user:pass@example.com/x") == "example.com"


def test_idna_equivalence() -> None:
    assert canonical_hostname("https://münchen.de/x") == "xn--mnchen-3ya.de"
    assert canonical_hostname("https://xn--mnchen-3ya.de/x") == "xn--mnchen-3ya.de"


def test_ip_literals() -> None:
    assert canonical_hostname("http://192.0.2.10/report") == "192.0.2.10"
    assert canonical_hostname("http://[2001:db8::1]/report") == "2001:db8::1"
    assert canonical_hostname("http://[2001:DB8::1]:8080/r") == "[2001:db8::1]:8080"


def test_paths_queries_fragments_ignored() -> None:
    assert (
        canonical_hostname("https://example.com/a/b?x=1&y=2#frag") == "example.com"
    )


def test_invalid_input_rejected() -> None:
    for bad in (
        "",
        "   ",
        "not a url",
        "example.com/no-scheme",
        "ftp://example.com/file",
        "file:///etc/hosts",
        "http://",
        "https://:8080/",
        "http://exam ple.com/",
        "http://example.com:abc/",
        "http://example.com:99999/",
        "http://ex..ample.com/",
    ):
        assert canonical_hostname(bad) == "", bad
    assert canonical_hostname(None) == ""  # type: ignore[arg-type]
    assert canonical_hostname(123) == ""  # type: ignore[arg-type]


def test_bare_hostname_text() -> None:
    assert canonical_host_text("Example.COM") == "example.com"
    assert canonical_host_text("example.com.") == "example.com"
    assert canonical_host_text("example.com:8080") == "example.com:8080"
    assert canonical_host_text("News.Example.COM:443") == "news.example.com"
    assert canonical_host_text("example.com:80") == "example.com"
    assert canonical_host_text("https://Example.COM/x") == "example.com"
    assert canonical_host_text("") == ""
    assert canonical_host_text("not a host!!") == ""


def test_evidence_host_prefers_valid_domain() -> None:
    assert (
        evidence_host("News.Example.COM", "https://news.example.com/x")
        == "news.example.com"
    )


def test_evidence_host_derives_from_url_when_domain_missing() -> None:
    assert evidence_host("", "https://Example.COM:443/x") == "example.com"
    assert evidence_host(None, "https://example.com/x") == "example.com"


def test_evidence_host_url_governs_over_conflicting_domain() -> None:
    assert evidence_host("trusted.gov", "https://evil.example/x") == "evil.example"
    assert evidence_host("other.example", "https://example.com/x") == "example.com"


def test_evidence_host_url_governs_over_garbage_domain() -> None:
    assert evidence_host("not a host!!", "https://example.com/x") == "example.com"


def test_evidence_host_domain_fallback_without_valid_url() -> None:
    assert evidence_host("Example.COM.", "not a url") == "example.com"
    assert evidence_host("example.com", "") == "example.com"
    assert evidence_host("example.com", None) == "example.com"
    assert evidence_host("", "not a url") == ""
    assert evidence_host("not a host!!", "not a url") == ""
    assert evidence_host("", "") == ""


def test_malformed_url_characters_rejected() -> None:
    for bad in (
        "http://example.com\n.evil.com/",
        "http://example.com\revil.com/",
        "http://example.com\tevil.com/",
        "http://exam ple.com/",
        "http://example.com\x00.evil.com/",
        "http://example.com\x1f/",
        "http://example.com\x7f/",
    ):
        assert canonical_hostname(bad) == "", repr(bad)
    assert canonical_host_text("exam\nple.com") == ""
    assert canonical_hostname("  https://example.com/  ") == "example.com"


def test_original_urls_untouched_by_parsing() -> None:
    from app.agents.gabriel.sources.brave import parse_results

    payload = {
        "web": {
            "results": [
                {
                    "title": "t",
                    "url": "https://News.Example.COM:443/Story?A=1",
                    "description": "s",
                }
            ]
        }
    }
    (result,) = parse_results(
        payload, assignment_id="gsp-1", query="q", retrieved_at="2026-01-01T00:00:00+00:00"
    )
    assert result.url == "https://News.Example.COM:443/Story?A=1"
    assert result.domain == "news.example.com"


def _ranked(urls: list[str], domains: list[str]):
    from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
    from app.agents.gabriel.ranking import rank_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult

    results = [
        extract_intelligence(
            BraveSearchResult(
                assignment_id="gsp-1",
                query="high-rise office building for sale in Dallas County, TX",
                title="Tower listed",
                url=url,
                snippet=(
                    "Beacon Tower at 100 Beacon St, Dallas, TX is listed for sale "
                    "with an asking price of $12 million. The Dallas County office "
                    "tower seeks buyers."
                ),
                domain=domain,
                provider="brave",
                retrieved_at="2026-10-27T12:00:00+00:00",
                published_at="2026-10-20",
                rank=index + 1,
            )
        )
        for index, (url, domain) in enumerate(zip(urls, domains))
    ]
    return rank_intelligence(ResearchIntelligence(results=results))


def test_ranking_equivalent_hosts_do_not_manufacture_corroboration() -> None:
    ranking = _ranked(
        ["https://News.Example.COM:443/a", "https://news.example.com/b"],
        ["News.Example.COM:443", "news.example.com"],
    )
    (candidate,) = ranking.candidates
    factors = {factor.factor: factor for factor in candidate.opportunity.factors}
    assert "2 distinct source domains" not in factors["evidence"].explanation


def test_ranking_distinct_hosts_still_corroborate() -> None:
    ranking = _ranked(
        ["https://news-a.example/story", "https://news-b.example/other"],
        ["news-a.example", "news-b.example"],
    )
    (candidate,) = ranking.candidates
    factors = {factor.factor: factor for factor in candidate.opportunity.factors}
    assert "2 distinct source domains" in factors["evidence"].explanation


def test_ranking_malformed_urls_cannot_corroborate() -> None:
    from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
    from app.agents.gabriel.ranking import rank_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult

    def _result(url: str, rank: int):
        return extract_intelligence(
            BraveSearchResult(
                assignment_id="gsp-1",
                query="q",
                title="t",
                url=url,
                snippet=(
                    "Beacon Tower at 100 Beacon St, Dallas, TX is listed for sale "
                    "with an asking price of $12 million. The Dallas County office "
                    "tower seeks buyers."
                ),
                domain="",
                provider="brave",
                retrieved_at="2026-10-27T12:00:00+00:00",
                published_at="2026-10-20",
                rank=rank,
            )
        )

    ranking = rank_intelligence(
        ResearchIntelligence(results=[_result("not a url", 1), _result("also bad", 2)])
    )
    assert len(ranking.candidates) <= 1
    if ranking.candidates:
        factors = {factor.factor: factor for factor in ranking.candidates[0].opportunity.factors}
        assert "2 distinct source domains" not in factors["evidence"].explanation


def test_dossier_equivalent_hosts_do_not_manufacture_corroboration() -> None:
    from app.agents.gabriel.dossier import build_dossiers
    from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult

    def _intel(url: str, domain: str, price: str):
        return extract_intelligence(
            BraveSearchResult(
                assignment_id="gsp-1",
                query="q",
                title="Tower listed",
                url=url,
                snippet=(
                    "Beacon Tower at 100 Beacon St, Dallas, TX is listed for sale "
                    f"with an asking price of {price}. The Dallas County office tower "
                    "seeks buyers."
                ),
                domain=domain,
                provider="brave",
                retrieved_at="2026-10-27T12:00:00+00:00",
                published_at="2026-10-20",
                rank=1,
            )
        )

    from app.agents.gabriel.dossier import DossierRequest

    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                ResearchIntelligence(
                    results=[
                        _intel("https://News.Example.COM/a", "News.Example.COM", "$12 million"),
                        _intel("https://news.example.com/b", "news.example.com", "$13 million"),
                    ]
                )
            ]
        )
    )
    (dossier,) = dossier_set.dossiers
    assert dossier.sources == [
        "https://News.Example.COM/a",
        "https://news.example.com/b",
    ]
    for claim in dossier.supported_facts:
        assert "2 distinct domains" not in claim.corroboration_note


def test_dossier_distinct_hosts_still_corroborate() -> None:
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult

    def _intel(url: str, domain: str, snippet: str):
        return extract_intelligence(
            BraveSearchResult(
                assignment_id="gsp-1",
                query="q",
                title="Tower listed",
                url=url,
                snippet=snippet,
                domain=domain,
                provider="brave",
                retrieved_at="2026-10-27T12:00:00+00:00",
                published_at="2026-10-20",
                rank=1,
            )
        )

    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                ResearchIntelligence(
                    results=[
                        _intel(
                            "https://news-a.example/x",
                            "news-a.example",
                            "Beacon Tower at 100 Beacon St, Dallas, TX is listed for sale "
                            "with an asking price of $12 million. The Dallas County office "
                            "tower seeks buyers.",
                        ),
                        _intel(
                            "https://news-b.example/y",
                            "news-b.example",
                            "Buyers seek the Dallas County office tower Beacon Tower, "
                            "100 Beacon St, Dallas, TX, listed for sale at an asking "
                            "price of $12 million.",
                        ),
                    ]
                )
            ]
        )
    )
    notes = [claim.corroboration_note for claim in dossier_set.dossiers[0].supported_facts]
    assert any("2 distinct domains" in note for note in notes)


def test_ranking_conflicting_domains_share_one_url_voice() -> None:
    ranking = _ranked(
        ["https://shared.example/a", "https://shared.example/b"],
        ["voice-a.example", "voice-b.example"],
    )
    (candidate,) = ranking.candidates
    factors = {factor.factor: factor for factor in candidate.opportunity.factors}
    assert "2 distinct source domains" not in factors["evidence"].explanation


def test_dossier_conflicting_domains_share_one_url_voice() -> None:
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.extractor import ResearchIntelligence, extract_intelligence
    from app.agents.gabriel.sources.brave import BraveSearchResult

    def _intel(url: str, domain: str, snippet: str):
        return extract_intelligence(
            BraveSearchResult(
                assignment_id="gsp-1",
                query="q",
                title="Tower listed",
                url=url,
                snippet=snippet,
                domain=domain,
                provider="brave",
                retrieved_at="2026-10-27T12:00:00+00:00",
                published_at="2026-10-20",
                rank=1,
            )
        )

    dossier_set = build_dossiers(
        DossierRequest(
            rounds=[
                ResearchIntelligence(
                    results=[
                        _intel(
                            "https://shared.example/a",
                            "voice-a.example",
                            "Beacon Tower at 100 Beacon St, Dallas, TX is listed for sale "
                            "with an asking price of $12 million. The Dallas County office "
                            "tower seeks buyers.",
                        ),
                        _intel(
                            "https://shared.example/b",
                            "voice-b.example",
                            "Buyers seek the Dallas County office tower Beacon Tower, "
                            "100 Beacon St, Dallas, TX, listed for sale at an asking "
                            "price of $12 million.",
                        ),
                    ]
                )
            ]
        )
    )
    (dossier,) = dossier_set.dossiers
    for claim in dossier.supported_facts:
        assert "2 distinct domains" not in claim.corroboration_note
