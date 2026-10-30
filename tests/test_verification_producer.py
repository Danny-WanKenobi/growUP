"""Trusted-producer tests (no network, no database, no registry curation)."""

from __future__ import annotations

from app.agents.gabriel.extractor import (
    ResearchIntelligence,
    extract_research_intelligence,
)
from app.agents.gabriel.producers import build_candidate_verifications
from app.agents.gabriel.ranking import rank_intelligence
from app.agents.gabriel.source_identity import canonical_hostname
from app.agents.gabriel.source_registry import (
    PRODUCTION_REGISTRY,
    RegistryEntry,
    SourceClass,
    SourceRegistry,
)
from app.agents.gabriel.sources.brave import BraveSearchResult
from app.agents.gabriel.verification import (
    VerificationFailureReason as R,
    VerificationOutcome,
)

_ADDR = "1200 Main Street, Dallas, TX 75202"


def _result(url: str, title: str, snippet: str, rank: int = 1) -> BraveSearchResult:
    return BraveSearchResult(
        assignment_id="a1",
        query="commercial property Dallas",
        title=title,
        url=url,
        snippet=snippet,
        domain=canonical_hostname(url),
        provider="brave",
        retrieved_at="2026-10-27T00:00:00+00:00",
        published_at=None,
        rank=rank,
    )


def _ranked(*results: BraveSearchResult):
    intel = extract_research_intelligence(list(results))
    return intel, rank_intelligence(intel)


def _only_candidate(intel, ranking):
    assert len(ranking.candidates) == 1
    return ranking.candidates[0]


def test_adapter_provenance_reaches_policy() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    # Adapter-collected voices: the caller-assertion failure is absent.
    assert R.CLIENT_ASSERTION_ONLY not in verdict.reasons
    assert [v.url for v in verdict.voices] == ["https://broker-a.example/x"]
    assert [v.host for v in verdict.voices] == ["broker-a.example"]


def test_missing_publishers_stay_unknown() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert verdict.voices
    assert all(v.publisher is None for v in verdict.voices)


def test_missing_listing_ids_stay_unknown() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert all(v.originating_id is None for v in verdict.voices)


def test_missing_facts_fail_closed_with_vetted_registry() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    registry = SourceRegistry(entries={
        "broker-a.example": RegistryEntry(
            hostname="broker-a.example",
            source_class=SourceClass.VETTED_COMMERCIAL,
            publisher="Broker A",
        ),
    })
    verdict = build_candidate_verifications(ranking, intel, registry)[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MISSING_DEFINING_FACTS in verdict.reasons


def test_distinct_hosts_do_not_verify() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}."),
        _result("https://broker-b.example/y", "Meridian Center",
               f"Office building for sale at {_ADDR}.", rank=2),
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    # Two hostnames, no vetted registry, no facts, no identity: the
    # evidence cannot verify, and multi-host agreement alone never does.
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.UNVETTED_SOURCES_ONLY in verdict.reasons
    assert R.REGISTRY_UNCONFIGURED in verdict.reasons
    assert R.IDENTITY_UNRESOLVED in verdict.reasons


def test_exact_address_grouping_does_not_align_identity() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    assert candidate.key.startswith("addr:")
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert R.IDENTITY_UNRESOLVED in verdict.reasons


def test_same_document_grouping_does_not_align_identity() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               "Commercial building in Dallas, TX."),
        _result("https://broker-a.example/x", "Meridian Center",
               "Commercial building in Dallas, TX.", rank=2),
    )
    candidate = _only_candidate(intel, ranking)
    assert candidate.key.startswith("doc:")
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert R.IDENTITY_UNRESOLVED in verdict.reasons


def test_identity_disputes_noted_not_resolved() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}."),
        _result("https://broker-b.example/y", "Meridian Plaza",
               f"Office building for sale at {_ADDR}.", rank=2),
    )
    candidate = _only_candidate(intel, ranking)
    assert candidate.identity_disputes.get("name")
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.IDENTITY_UNRESOLVED in verdict.reasons


def test_empty_production_registry_blocks() -> None:
    assert PRODUCTION_REGISTRY.entries == {}
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert R.REGISTRY_UNCONFIGURED in verdict.reasons


def test_ranking_scores_and_order_unchanged() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}."),
        _result("https://broker-b.example/y", "Meridian Plaza",
               f"Office building for sale at {_ADDR}.", rank=2),
    )
    before = ranking.model_dump(mode="json")
    verdicts = build_candidate_verifications(ranking, intel)
    assert set(verdicts) == {c.key for c in ranking.candidates}
    # The producer annotates via its return value; the ranking is untouched.
    assert ranking.model_dump(mode="json") == before


def test_category_attribution_untouched() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    build_candidate_verifications(ranking, intel)
    assert all(c.category_attribution is None for c in ranking.candidates)
    assert all(c.category_verification is None for c in ranking.candidates)


def test_direct_ranking_carries_no_provenance() -> None:
    # rank_intelligence is shared by dossier/investigation flows over
    # caller-submitted intelligence: it must never stamp provenance itself.
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    assert all(c.category_verification is None for c in ranking.candidates)


def test_dossiers_carry_no_verification_annotation() -> None:
    from app.agents.gabriel.dossier import DossierRequest, PropertyDossier, build_dossiers

    intel, _ = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    assert "category_verification" not in PropertyDossier.model_fields
    assert dossier_set.dossiers


def test_contradictory_categories_noted() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Tower",
               f"Twelve-story office tower for sale at {_ADDR}."),
        _result("https://broker-b.example/y", "Meridian Warehouse",
               f"Warehouse for sale at {_ADDR}.", rank=2),
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MATERIAL_CONTRADICTION in verdict.reasons


def test_verdicts_deterministic_under_reordering() -> None:
    first = [
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}."),
        _result("https://broker-b.example/y", "Meridian Center",
               f"Office building for sale at {_ADDR}.", rank=2),
    ]
    intel_a, ranking_a = _ranked(*first)
    intel_b, ranking_b = _ranked(*reversed(first))
    verdicts_a = build_candidate_verifications(ranking_a, intel_a)
    verdicts_b = build_candidate_verifications(ranking_b, intel_b)
    assert verdicts_a.keys() == verdicts_b.keys()
    for key in verdicts_a:
        assert verdicts_a[key].model_dump_json() == verdicts_b[key].model_dump_json()


def test_empty_ranking_yields_no_verdicts() -> None:
    intel = ResearchIntelligence(results=[])
    ranking = rank_intelligence(intel)
    assert ranking.candidates == []
    assert build_candidate_verifications(ranking, intel) == {}


def test_research_run_annotates_compatibly(monkeypatch) -> None:
    import app.agents.gabriel.research as research
    from app.agents.gabriel.planner import plan_initial_searches
    from app.observability.emitter import BatchEmitOutcome

    for var, value in (("BRAVE_SEARCH_API_KEY", "unit-test-brave-key-only"),
                       ("BRAVE_MONTHLY_CEILING", "100"),
                       ("DATABASE_HOST", "localhost"), ("DATABASE_PORT", "5432"),
                       ("DATABASE_NAME", "db"), ("DATABASE_USER", "u"),
                       ("DATABASE_PASSWORD", "pw")):
        monkeypatch.setenv(var, value)

    payloads = [
        ("https://broker-a.example/x", "Meridian Center",
         f"Office building for sale at {_ADDR}."),
        ("https://broker-b.example/y", "Meridian Center",
         f"Office building for sale at {_ADDR}."),
    ]

    def _execute(query, assignment_id, timeout):
        return [
            BraveSearchResult(
                assignment_id=assignment_id, query=query, title=title, url=url,
                snippet=snippet, domain=canonical_hostname(url), provider="brave",
                retrieved_at="2026-10-27T00:00:00+00:00", published_at=None, rank=i + 1,
            )
            for i, (url, title, snippet) in enumerate(payloads)
        ]

    monkeypatch.setattr(research, "execute_query", _execute)
    spent = {"used": 0}

    def _reserve(n):
        spent["used"] += n
        return {"used": spent["used"], "ceiling": 100,
                "remaining": 100 - spent["used"], "month": "2026-10"}

    monkeypatch.setattr(research, "reserve_requests", _reserve)
    # Verdict persistence is covered elsewhere; this test owns annotation.
    monkeypatch.setattr(
        research, "persist_verdicts", lambda verdicts: BatchEmitOutcome(pending=False)
    )
    request = research.ResearchRunRequest.model_validate(
        {"assignments": [a.model_dump() for a in plan_initial_searches()],
         "include_ranking": True}
    )
    result = research.run_research(request)
    assert result.ranking is not None
    assert result.ranking.candidates
    for candidate in result.ranking.candidates:
        assert candidate.category_verification is not None
        assert candidate.category_verification.outcome is VerificationOutcome.NOT_VERIFIED
        assert candidate.category_attribution is None
    # Scores and orders match a direct ranking of the same intelligence.
    direct = rank_intelligence(result.intelligence)
    assert result.ranking.value_order == direct.value_order
    assert result.ranking.opportunity_order == direct.opportunity_order
    assert [c.opportunity.score for c in result.ranking.candidates] == [
        c.opportunity.score for c in direct.candidates
    ]
    # Response serialization round-trips with the additive annotation.
    assert "category_verification" in result.model_dump_json()
