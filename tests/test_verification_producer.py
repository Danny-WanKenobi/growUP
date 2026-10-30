"""Trusted-producer tests (no network, no database, no registry curation)."""

from __future__ import annotations

import json
import os
from uuid import uuid4

import pytest

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


def _vetted_registry(*hosts: str) -> SourceRegistry:
    return SourceRegistry(entries={
        host: RegistryEntry(
            hostname=host,
            source_class=SourceClass.VETTED_COMMERCIAL,
            publisher=f"Publisher {host}",
        )
        for host in hosts
    })


def test_missing_facts_fail_closed_with_vetted_registry() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office property for sale at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(
        ranking, intel, _vetted_registry("broker-a.example"))[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MISSING_DEFINING_FACTS in verdict.reasons


def test_extracted_facts_flow_into_eligibility() -> None:
    # A use fact makes the voice eligible, advancing past
    # MISSING_DEFINING_FACTS -- while staying NOT_VERIFIED overall.
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(
        ranking, intel, _vetted_registry("broker-a.example"))[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MISSING_DEFINING_FACTS not in verdict.reasons
    assert R.SINGLE_VOICE_ONLY in verdict.reasons
    assert R.IDENTITY_UNRESOLVED in verdict.reasons


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


# --- Phase 2B: verification persistence in research verdicts ---

_LIVE_PG = os.getenv("GROWUP_PG_INTEGRATION") == "1"
needs_live_pg = pytest.mark.skipif(not _LIVE_PG, reason="GROWUP_PG_INTEGRATION=1 not set")


def _annotated():
    from app.agents.gabriel.producers import build_candidate_verifications

    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}."),
        _result("https://broker-b.example/y", "Meridian Center",
               f"Office building for sale at {_ADDR}.", rank=2),
    )
    annotations = build_candidate_verifications(ranking, intel)
    for candidate in ranking.candidates:
        candidate.category_verification = annotations[candidate.key]
    return intel, ranking


def test_verdict_carries_not_verified() -> None:
    from app.agents.gabriel.provenance import build_research_verdicts

    intel, ranking = _annotated()
    (verdict,) = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    stored = verdict.details["category_verification"]
    assert stored["outcome"] == "not_verified"
    assert stored["pathway"] is None
    assert isinstance(stored["reasons"], list) and stored["reasons"]
    assert stored["rationale"]
    assert isinstance(stored["limitations"], list)
    assert [v["url"] for v in stored["voices"]] == [
        "https://broker-a.example/x", "https://broker-b.example/y"]
    assert [v["host"] for v in stored["voices"]] == [
        "broker-a.example", "broker-b.example"]


def test_verdict_details_deterministic_json() -> None:
    from app.agents.gabriel.provenance import build_research_verdicts

    intel, ranking = _annotated()
    first = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    second = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    assert json.dumps(first[0].details, sort_keys=True) == json.dumps(
        second[0].details, sort_keys=True)


def test_missing_annotation_preserves_details() -> None:
    from app.agents.gabriel.provenance import build_research_verdicts

    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    (verdict,) = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    assert "category_verification" not in verdict.details
    for key in ("decision", "reason_codes", "candidate_ref", "observed",
                "inferred", "result", "mutation_status"):
        assert key in verdict.details


def test_screened_verdicts_carry_no_verification() -> None:
    from app.agents.gabriel.provenance import build_research_verdicts

    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Unrelated story",
               "Downtown tower for sale.")
    )
    assert ranking.candidates == [] and len(ranking.screened_leads) == 1
    (verdict,) = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    assert verdict.details["decision"] == "SCREENED"
    assert "category_verification" not in verdict.details


def test_dossier_verdicts_carry_no_verification() -> None:
    from app.agents.gabriel.dossier import DossierRequest, build_dossiers
    from app.agents.gabriel.provenance import build_dossier_verdicts

    intel, _ = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building for sale at {_ADDR}.")
    )
    dossier_set = build_dossiers(DossierRequest(rounds=[intel]))
    items = [item for intel_round in [intel] for item in intel_round.results]
    verdicts = build_dossier_verdicts(
        run_id="run-2b", correlation_id="", dossier_set=dossier_set, items=items)
    assert verdicts
    assert all("category_verification" not in v.details for v in verdicts)


def test_ranking_unchanged_by_verdict_build() -> None:
    from app.agents.gabriel.provenance import build_research_verdicts

    intel, ranking = _annotated()
    before = ranking.model_dump(mode="json")
    build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    assert ranking.model_dump(mode="json") == before


def test_persisted_details_show_empty_registry() -> None:
    from app.agents.gabriel.provenance import build_research_verdicts

    intel, ranking = _annotated()
    (verdict,) = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    stored = verdict.details["category_verification"]
    assert stored["outcome"] == "not_verified"
    assert "registry_unconfigured" in stored["reasons"]
    assert "identity_unresolved" in stored["reasons"]


def test_persisted_details_show_contradiction() -> None:
    from app.agents.gabriel.producers import build_candidate_verifications
    from app.agents.gabriel.provenance import build_research_verdicts

    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Tower",
               f"Twelve-story office tower for sale at {_ADDR}."),
        _result("https://broker-b.example/y", "Meridian Warehouse",
               f"Warehouse for sale at {_ADDR}.", rank=2),
    )
    annotations = build_candidate_verifications(ranking, intel)
    for candidate in ranking.candidates:
        candidate.category_verification = annotations[candidate.key]
    (verdict,) = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    assert "material_contradiction" in verdict.details["category_verification"]["reasons"]


def test_persisted_details_contain_no_fabricated_facts() -> None:
    from app.agents.gabriel.provenance import build_research_verdicts

    intel, ranking = _annotated()
    (verdict,) = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    for voice in verdict.details["category_verification"]["voices"]:
        assert voice["publisher"] is None
        assert voice["originating_id"] is None
        assert voice["source_class"] == "unvetted"


def test_outbox_round_trip_preserves_verification(tmp_path) -> None:
    from app.agents.gabriel.provenance import build_research_verdicts
    from app.observability.outbox import Outbox

    intel, ranking = _annotated()
    (verdict,) = build_research_verdicts(
        run_id="run-2b", correlation_id="", ranking=ranking, intel=intel)
    box = Outbox(tmp_path / "outbox.sqlite3")
    box.initialize()
    box.accept_all([verdict])
    stored = box.get(verdict.event_id)
    assert stored is not None
    assert stored.details["category_verification"]["outcome"] == "not_verified"
    assert stored.details["category_verification"]["rationale"] == (
        verdict.details["category_verification"]["rationale"])


@needs_live_pg
def test_live_verdict_persists_category_verification() -> None:
    import app.properties_db as pdb
    from app.agents.gabriel.provenance import build_research_verdicts
    from app.observability.store import insert_event, query_events

    intel, ranking = _annotated()
    run_id = f"live-verify-2b-{uuid4().hex}"
    verdicts = build_research_verdicts(
        run_id=run_id, correlation_id="", ranking=ranking, intel=intel)
    assert len(verdicts) == 1
    try:
        insert_event(verdicts[0])
        events = query_events(
            run_id=run_id, event_type="gabriel.research.candidate_decision")
        assert len(events) == 1
        stored = events[0].details["category_verification"]
        assert stored["outcome"] == "not_verified"
        assert "registry_unconfigured" in stored["reasons"]
        assert stored["rationale"]
    finally:
        with pdb.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_events WHERE run_id = %(run_id)s",
                            {"run_id": run_id})
            conn.commit()


# --- Phase 2C: fact mapping safeguards ---


def test_proposed_specs_excluded_from_high_rise_facts() -> None:
    # "Proposed 20-story tower": specs stay observations but must not
    # become positive current-building verification facts.
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Tower",
               f"Proposed 20-story tower at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    assert any(ob.fact == "levels" for item in intel.results for ob in item.building_facts)
    verdict = build_candidate_verifications(
        ranking, intel, _vetted_registry("broker-a.example"))[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MISSING_DEFINING_FACTS in verdict.reasons


def test_planned_and_under_construction_excluded() -> None:
    for snippet in ("Planned 90-meter tower at %s." % _ADDR,
                    "Tower under construction, 25 stories, at %s." % _ADDR):
        intel, ranking = _ranked(
            _result("https://broker-a.example/x", "Meridian Tower", snippet)
        )
        candidate = _only_candidate(intel, ranking)
        verdict = build_candidate_verifications(
            ranking, intel, _vetted_registry("broker-a.example"))[candidate.key]
        assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
        assert R.MISSING_DEFINING_FACTS in verdict.reasons


def test_existing_high_rise_facts_flow_but_do_not_verify() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Tower",
               f"Existing 20-story office tower at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    # Empty production registry: facts alone never verify.
    verdict = build_candidate_verifications(ranking, intel)[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.REGISTRY_UNCONFIGURED in verdict.reasons
    # Even vetted, a single unaligned voice cannot verify.
    vetted = build_candidate_verifications(
        ranking, intel, _vetted_registry("broker-a.example"))[candidate.key]
    assert vetted.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MISSING_DEFINING_FACTS not in vetted.reasons
    assert R.SINGLE_VOICE_ONLY in vetted.reasons


def test_within_voice_conflict_drops_key_and_marks_contradiction() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Center",
               f"Office building and warehouse complex at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(
        ranking, intel, _vetted_registry("broker-a.example"))[candidate.key]
    assert verdict.outcome is VerificationOutcome.NOT_VERIFIED
    assert R.MATERIAL_CONTRADICTION in verdict.reasons


def test_ambiguous_status_fails_closed_to_proposed() -> None:
    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Meridian Tower",
               f"Existing tower, proposed 30-story expansion at {_ADDR}.")
    )
    candidate = _only_candidate(intel, ranking)
    verdict = build_candidate_verifications(
        ranking, intel, _vetted_registry("broker-a.example"))[candidate.key]
    # Proposed wins: high-rise specs excluded, no current-building facts.
    assert R.MISSING_DEFINING_FACTS in verdict.reasons


def test_multi_property_facts_not_attributed() -> None:
    from app.agents.gabriel.producers import _voice_facts

    intel, ranking = _ranked(
        _result("https://broker-a.example/x", "Two properties",
               "Meridian Center at 1200 Main Street and Meridian Plaza at "
               "1300 Main Street, Dallas, TX 75202. 12-story office building.")
    )
    assert len(intel.results[0].properties) > 1
    facts, _ = _voice_facts(intel.results[0])
    assert facts == {}


def test_facts_do_not_change_ranking_or_response(monkeypatch) -> None:
    import app.agents.gabriel.research as research
    from app.agents.gabriel.planner import plan_initial_searches
    from app.observability.emitter import BatchEmitOutcome

    for var, value in (("BRAVE_SEARCH_API_KEY", "unit-test-brave-key-only"),
                       ("BRAVE_MONTHLY_CEILING", "100"),
                       ("DATABASE_HOST", "localhost"), ("DATABASE_PORT", "5432"),
                       ("DATABASE_NAME", "db"), ("DATABASE_USER", "u"),
                       ("DATABASE_PASSWORD", "pw")):
        monkeypatch.setenv(var, value)

    def _execute(query, assignment_id, timeout):
        return [_result("https://broker-a.example/x", "Meridian Tower",
                        f"Existing 20-story office tower at {_ADDR}.")]

    monkeypatch.setattr(research, "execute_query", _execute)
    monkeypatch.setattr(research, "reserve_requests",
                        lambda n: {"used": n, "ceiling": 100,
                                   "remaining": 100 - n, "month": "2026-10"})
    monkeypatch.setattr(
        research, "persist_verdicts", lambda verdicts: BatchEmitOutcome(pending=False))
    request = research.ResearchRunRequest.model_validate(
        {"assignments": [a.model_dump() for a in plan_initial_searches()],
         "include_ranking": True}
    )
    result = research.run_research(request)
    direct = rank_intelligence(result.intelligence)
    assert result.ranking.value_order == direct.value_order
    assert result.ranking.opportunity_order == direct.opportunity_order
    assert [c.opportunity.score for c in result.ranking.candidates] == [
        c.opportunity.score for c in direct.candidates
    ]
    for candidate in result.ranking.candidates:
        assert candidate.category_verification is not None
        assert candidate.category_verification.outcome is VerificationOutcome.NOT_VERIFIED
    assert "building_facts" in result.model_dump_json()
